"""Local procurement workspace in the customer's EKT visual identity."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pandas as pd
import streamlit as st

from branding import ASSETS, render_brand, render_empty, render_footer, render_methodology
from procurement.calculate import Settings, recommend
from procurement.ingest import load_archive


st.set_page_config(page_title='План закупок | Электрокомплект',
                   page_icon=str(ASSETS / 'ekt-logo.svg'), layout='wide')
render_brand()


def read_optional(uploaded, required, label):
    if uploaded is None:
        return pd.DataFrame()
    frame = pd.read_csv(io.BytesIO(uploaded.getvalue()), sep=None, engine='python',
                        encoding='utf-8-sig', dtype={'code': str})
    if not set(required).issubset(frame.columns):
        raise ValueError(f'{label}: требуются колонки {", ".join(required)}')
    frame['code'] = frame['code'].str.strip()
    return frame


@st.cache_data(show_spinner=False)
def parse_archive(content: bytes, name: str):
    return load_archive(content, name)


def calculate(uploads, balance_upload, stockout_upload, review, safety, growth):
    balances = read_optional(balance_upload, ['supplier', 'code', 'balance'], 'Остатки')
    stockouts = read_optional(stockout_upload, ['supplier', 'code', 'start', 'end'], 'Stockout')
    if not balances.empty:
        balances['balance'] = pd.to_numeric(balances.balance, errors='raise')
        if (balances.balance < 0).any() or balances.duplicated(['supplier', 'code']).any():
            raise ValueError('Остатки должны быть неотрицательными, а пары supplier/code — уникальными')
    if not stockouts.empty:
        for field in ['start', 'end']:
            stockouts[field] = pd.to_datetime(stockouts[field], format='%Y-%m-%d', errors='raise').dt.date
    results, issues, dates = [], [], []
    for name, upload, lead in uploads:
        if upload is None:
            continue
        dataset = parse_archive(upload.getvalue(), name)
        settings = Settings(int(lead), int(review), int(safety), growth / 100)
        own_balances = balances[balances.supplier == name] if not balances.empty else balances
        own_stockouts = stockouts[stockouts.supplier == name] if not stockouts.empty else stockouts
        results.append(recommend(dataset, settings, own_stockouts, own_balances))
        issues.extend(dataset.issues)
        dates.append(f'{name}: {dataset.as_of.isoformat()}')
    return {'rows': pd.concat(results, ignore_index=True) if results else pd.DataFrame(),
            'issues': list(dict.fromkeys(issues)), 'dates': dates}


def remember_edits(editor_key, row_ids):
    for position, changes in st.session_state[editor_key].get('edited_rows', {}).items():
        if 'Рекомендовано' in changes:
            st.session_state.order_quantities[row_ids[int(position)]] = changes['Рекомендовано']


def render_results(result):
    all_rows = result['rows']
    if all_rows.empty:
        st.warning('Нет позиций с известным остатком. Загрузите актуальные остатки CSV.')
        return
    st.html('<div class="ekt-result-heading" id="order-results"><h2>Результат расчёта</h2>'
            '<span>Готов к проверке</span></div>')
    st.caption('Даты последних операций: ' + ' · '.join(result['dates']))
    left, middle, right = st.columns(3)
    left.metric('Позиций в расчёте', f'{len(all_rows):,}'.replace(',', ' '))
    middle.metric('Позиций к заказу', f'{(all_rows["Рекомендовано"] > 0).sum():,}'.replace(',', ' '))
    right.metric('Высокий риск дефицита',
                 f'{((all_rows["Срочность"] == "Высокая") & (all_rows["Рекомендовано"] > 0)).sum():,}'.replace(',', ' '))
    demo = all_rows['Источник остатка'].str.contains('демонстрацион|сводной', case=False, regex=True).any()
    if demo:
        st.warning('Проверьте остатки: часть данных не подтверждена на дату расчёта. '
                   'Перед рабочим заказом загрузите актуальные остатки.')
    with st.container(key='order_table'):
        st.html('<div class="ekt-table-heading"><h3>Проект заказа</h3>'
                '<p>Проверьте рекомендации и при необходимости измените количество.</p></div>')
        search_column, supplier_column = st.columns([1.6, 1])
        search = search_column.text_input('Поиск по товарам', placeholder='Название, артикул или код 1С',
                                          label_visibility='collapsed', key='product_search')
        supplier = supplier_column.selectbox('Поставщик', ['Все поставщики', *sorted(all_rows['Поставщик'].unique())],
                                              label_visibility='collapsed', key='supplier_filter')
        only_orders = st.toggle('Только позиции к заказу', value=True, key='only_orders')
        view = all_rows.copy()
        if supplier != 'Все поставщики':
            view = view[view['Поставщик'] == supplier]
        if only_orders:
            view = view[view['Рекомендовано'] > 0]
        if search.strip():
            matches = view[['Наименование', 'Артикул', 'Код 1С']].fillna('').astype(str).apply(
                lambda col: col.str.contains(search.strip(), case=False, regex=False))
            view = view[matches.any(axis=1)]
        if view.empty:
            st.info('По выбранным условиям позиций нет. Измените поиск или фильтры.')
            return
        row_ids = tuple(zip(view['Поставщик'], view['Код 1С']))
        previous_editor = f'order_editor_{st.session_state.get("editor_version", 0)}'
        if st.session_state.get('visible_order_ids') != row_ids or previous_editor not in st.session_state:
            st.session_state.visible_order_ids = row_ids
            for index, row_id in zip(view.index, row_ids):
                if row_id in st.session_state.order_quantities:
                    view.at[index, 'Рекомендовано'] = st.session_state.order_quantities[row_id]
            st.session_state.visible_order_rows = view
            st.session_state.editor_version = st.session_state.get('editor_version', 0) + 1
        editor_key = f'order_editor_{st.session_state.editor_version}'
        edited = st.data_editor(
            st.session_state.visible_order_rows, key=editor_key, hide_index=True,
            width='stretch', height=min(520, 38 + len(view) * 35), num_rows='fixed',
            on_change=remember_edits, args=(editor_key, row_ids),
            disabled=[c for c in view.columns if c != 'Рекомендовано'],
            column_order=['Код 1С', 'Наименование', 'Рекомендовано', 'Кратность', 'Срочность',
                          'Поставщик', 'Артикул', 'Прогноз', 'Остаток', 'В пути вовремя',
                          'Резерв', 'Обоснование', 'Категория', 'В пути без даты', 'Тренд',
                          'Исключено выбросов', 'Упущенный спрос', 'Источник остатка'],
            column_config={
                'Наименование': st.column_config.TextColumn(width='medium'),
                'Рекомендовано': st.column_config.NumberColumn('К заказу, шт.', min_value=0,
                                                               step=1, required=True, width=120),
                'Обоснование': st.column_config.TextColumn(width='large'),
            },
        )
        st.caption(f'Показано позиций: {len(edited)}. Количество должно соответствовать кратности. '
                   'Ручная правка не меняет прогноз. В CSV попадут строки текущего фильтра.')
        wrong = edited.apply(lambda r: pd.isna(r['Рекомендовано']) or
                             int(r['Рекомендовано']) % int(r['Кратность']) != 0, axis=1)
        if wrong.any():
            st.error(f'{wrong.sum()} количеств не соответствуют кратности. Исправьте их перед экспортом.')
        else:
            st.download_button('Скачать проект заказа · CSV',
                               edited.to_csv(index=False, sep=';').encode('utf-8-sig'),
                               file_name='zakazy_postavshchikam.csv', mime='text/csv',
                               type='primary', on_click='ignore')
    if result['issues']:
        with st.expander(f'Проверка исходных данных · {len(result["issues"])} замечаний'):
            for warning in result['issues']:
                st.write('• ' + warning)


inputs, workspace = st.columns([1, 2.5], gap='large')
with inputs:
    with st.container(key='controls'):
        st.html('<div class="ekt-panel-title" id="input-data"><span>01</span><h2>Данные поставщиков</h2></div>'
                '<p class="ekt-panel-note">Загрузите один или оба ZIP-архива.</p>')
        iek_upload = st.file_uploader('IEK', type='zip', key='iek', help='Архив IEK.zip, распаковывать не нужно.')
        system_upload = st.file_uploader('Systeme Electric', type='zip', key='system')
        with st.expander('Дополнительные данные · CSV'):
            balance_upload = st.file_uploader('Актуальные остатки', type='csv', key='balance')
            stockout_upload = st.file_uploader('Периоды отсутствия товара', type='csv', key='stockout')
            st.caption('Остатки: supplier,code,balance. Периоды отсутствия: supplier,code,start,end; '
                       'даты ГГГГ-ММ-ДД. Поставщик: IEK или Systeme Electric.')
        st.html('<hr class="ekt-input-divider"><div class="ekt-panel-title"><span>02</span><h2>Параметры расчёта</h2></div>')
        lead_iek = st.number_input('Поставка IEK, дней', 1, 365, 30)
        lead_system = st.number_input('Поставка Systeme Electric, дней', 1, 365, 30)
        with st.expander('Запас и прирост спроса'):
            review = st.number_input('Интервал между расчётами, дней', 1, 90, 7)
            safety = st.number_input('Страховой запас, дней спроса', 0, 90, 7)
            growth = st.number_input('Дополнительный прирост спроса, %', -50, 200, 0)
        st.caption('Сроки 30 дней заданы для демонстрации. Укажите реальные сроки поставки.')
        run = st.button('Рассчитать заказ', type='primary', width='stretch')
        st.caption('Файлы обрабатываются локально на вашем компьютере.')

signature = tuple(hashlib.sha256(upload.getvalue()).hexdigest() if upload else None
                  for upload in (iek_upload, system_upload, balance_upload, stockout_upload))
signature += (lead_iek, lead_system, review, safety, growth)

with workspace:
    if run:
        if not iek_upload and not system_upload:
            st.warning('Для расчёта загрузите хотя бы один ZIP-архив слева.')
        else:
            try:
                with st.spinner('Анализируем продажи и рассчитываем потребность…'):
                    result = calculate([('IEK', iek_upload, lead_iek),
                                        ('Systeme Electric', system_upload, lead_system)],
                                       balance_upload, stockout_upload, review, safety, growth)
                st.session_state.order_result = result
                st.session_state.order_signature = signature
                st.session_state.order_quantities = {}
                st.session_state.visible_order_ids = None
            except (ValueError, KeyError, IndexError, OSError, zipfile.BadZipFile, pd.errors.ParserError) as error:
                st.error(f'Не удалось обработать данные: {error}')
    if 'order_result' in st.session_state and st.session_state.order_signature == signature:
        render_results(st.session_state.order_result)
    else:
        if 'order_result' in st.session_state:
            st.info('Данные или параметры изменились. Нажмите «Рассчитать заказ», чтобы обновить результат.')
        render_empty()
    render_methodology()

render_footer()
