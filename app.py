"""Local procurement workspace in the customer's EKT visual identity."""

from __future__ import annotations

from datetime import date, timedelta
import io
import sqlite3
import zipfile

import pandas as pd
import streamlit as st

from branding import ASSETS, render_brand, render_empty, render_footer, render_methodology
from bi_ui import render_bi
from procurement.calculate import MODEL_VERSION, Settings, recommend
from procurement.ingest import load_archive
from procurement.storage import Store


st.set_page_config(page_title='План закупок | Электрокомплект',
                   page_icon=str(ASSETS / 'ekt-logo.svg'), layout='wide')
store = Store()
preferences = store.get_preferences()


def restore_run(run_id, restore_inputs=False):
    saved = store.get_run(int(run_id))
    st.session_state.order_result = saved['result']
    st.session_state.order_run_id = saved['id']
    st.session_state.order_signature = tuple(saved['metadata']['signature'])
    st.session_state.order_quantities = {
        (row.supplier, row.code): row.quantity for row in saved['edits'].itertuples()
    }
    st.session_state.visible_order_ids = None
    st.session_state.pop('order_save_error', None)
    if restore_inputs:
        preferences.update(saved['metadata']['params'])
        preferences['sources'] = saved['metadata']['sources']
        preferences['last_run_id'] = saved['id']
        store.save_preferences(preferences)
        st.session_state.input_generation = st.session_state.get('input_generation', 0) + 1
        for key in saved['metadata']['params']:
            st.session_state.pop(key, None)
        for key in list(st.session_state):
            if key.startswith('saved_source_'):
                del st.session_state[key]


def request_open_run(run_id):
    st.session_state.requested_run_id = run_id


if not st.session_state.get('workspace_initialized'):
    if preferences.get('last_run_id'):
        try:
            restore_run(preferences['last_run_id'])
        except (KeyError, ValueError):
            pass
    st.session_state.workspace_initialized = True
if 'requested_run_id' in st.session_state:
    restore_run(st.session_state.pop('requested_run_id'), restore_inputs=True)
    st.session_state.workspace_page = 'План закупок'

page_name = st.session_state.get('workspace_page', 'План закупок')
render_brand(page_name)
st.radio('Раздел приложения', ['План закупок', 'BI-аналитика'], horizontal=True,
         key='workspace_page', label_visibility='collapsed')


class SavedUpload(io.BytesIO):
    def __init__(self, source):
        super().__init__(source['content'])
        self.name = source['filename']
        self.source_id = source['id']


def choose_source(label, kind, supplier, input_key, file_type, help_text=None):
    upload = st.file_uploader(label, type=file_type,
                              key=f'{input_key}_{st.session_state.get("input_generation", 0)}',
                              help=help_text)
    if upload is not None:
        filename = getattr(upload, 'name', f'{input_key}.{file_type}')
        source_id = store.save_source(kind, supplier, filename, upload.getvalue())
        st.caption('Файл сохранён. Он будет доступен после перезапуска.')
        return SavedUpload(store.get_source(source_id))
    sources = store.list_sources(kind=kind, supplier=supplier)
    if sources.empty:
        return None
    sources = sources.sort_values('uploaded_at', ascending=False)
    labels = {row.id: f'{row.filename} · {str(row.uploaded_at)[:16].replace("T", " ")} · {row.id[:6]}'
              for row in sources.itertuples()}
    options = [None, *labels]
    selected = preferences.get('sources', {}).get(input_key)
    source_id = st.selectbox(f'Сохранённый файл · {label}', options,
                             index=options.index(selected) if selected in options else 0,
                             format_func=lambda value: labels.get(value, 'Не использовать'),
                             key=f'saved_source_{input_key}')
    return SavedUpload(store.get_source(source_id)) if source_id else None


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


@st.cache_data(show_spinner=False)
def read_dataset(database_path, dataset_id):
    return Store(database_path).get_dataset(dataset_id)


def load_dataset(upload, supplier):
    known = store.list_datasets()
    if not known.empty:
        matching = known[known.source_id == upload.source_id]
        if not matching.empty:
            dataset_id = int(matching.iloc[-1]['id'])
            return dataset_id, read_dataset(str(store.path), dataset_id)
    with st.spinner(f'Читаем и сохраняем архив {supplier}…'):
        dataset = parse_archive(upload.getvalue(), supplier)
        dataset_id = store.save_dataset(upload.source_id, dataset)
    return dataset_id, dataset


def calculate(uploads, balance_upload, stockout_upload, review, safety, growth, forecast_start, datasets):
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
        _, dataset = datasets[name]
        settings = Settings(int(lead), int(review), int(safety), growth / 100)
        own_balances = balances[balances.supplier == name] if not balances.empty else balances
        own_stockouts = stockouts[stockouts.supplier == name] if not stockouts.empty else stockouts
        results.append(recommend(dataset, settings, own_stockouts, own_balances,
                                 forecast_start=forecast_start))
        issues.extend(dataset.issues)
        dates.append(f'{name}: {dataset.as_of.isoformat()}')
    return {'rows': pd.concat(results, ignore_index=True) if results else pd.DataFrame(),
            'issues': list(dict.fromkeys(issues)), 'dates': dates}


def remember_edits(editor_key, row_ids, run_id):
    errors = []
    for position, changes in st.session_state[editor_key].get('edited_rows', {}).items():
        if 'Рекомендовано' in changes:
            identity = row_ids[int(position)]
            quantity = changes['Рекомендовано']
            st.session_state.order_quantities[identity] = quantity
            if quantity is not None:
                try:
                    store.save_order_quantity(run_id, identity[0], identity[1], quantity)
                except (ValueError, sqlite3.Error) as error:
                    errors.append(f'Не удалось сохранить правку: {error}')
            else:
                errors.append('Пустое количество не сохранено. Укажите число, в том числе 0, если заказ не нужен.')
    if errors:
        st.session_state.order_save_error = ' '.join(dict.fromkeys(errors))
    else:
        st.session_state.pop('order_save_error', None)


def render_results(result):
    all_rows = result['rows'].copy()
    for index, row in all_rows.iterrows():
        identity = (row['Поставщик'], row['Код 1С'])
        if identity in st.session_state.order_quantities:
            all_rows.at[index, 'Рекомендовано'] = st.session_state.order_quantities[identity]
    if all_rows.empty:
        st.warning('Нет позиций с известным остатком. Загрузите актуальные остатки CSV.')
        return
    st.html('<div class="ekt-result-heading" id="order-results"><h2>Результат расчёта</h2>'
            '<span>Готов к проверке</span></div>')
    st.caption('Даты последних операций: ' + ' · '.join(result['dates']))
    st.caption(f'Расчёт №{st.session_state.order_run_id} сохранён. Ручные количества сохраняются автоматически.')
    if st.session_state.get('order_save_error'):
        st.error(st.session_state.order_save_error)
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
            on_change=remember_edits, args=(editor_key, row_ids, st.session_state.order_run_id),
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


if st.session_state.workspace_page == 'BI-аналитика':
    render_bi(store, on_open_run=request_open_run)
    render_footer()
    st.stop()

inputs, workspace = st.columns([1, 2.5], gap='large')
datasets = {}
source_errors = []
with inputs:
    with st.container(key='controls'):
        st.html('<div class="ekt-panel-title" id="input-data"><span>01</span><h2>Данные поставщиков</h2></div>'
                '<p class="ekt-panel-note">Загрузите архив или выберите сохранённый.</p>')
        iek_upload = choose_source('IEK', 'archive', 'IEK', 'iek', 'zip',
                                   'Архив IEK.zip. Распаковывать не нужно.')
        system_upload = choose_source('Systeme Electric', 'archive', 'Systeme Electric', 'system', 'zip')
        with st.expander('Дополнительные данные · CSV'):
            balance_upload = choose_source('Актуальные остатки', 'balances', 'Все поставщики', 'balance', 'csv')
            stockout_upload = choose_source('Периоды отсутствия товара', 'stockouts', 'Все поставщики', 'stockout', 'csv')
            st.caption('Остатки: supplier,code,balance. Периоды отсутствия: supplier,code,start,end; '
                       'даты ГГГГ-ММ-ДД. Поставщик: IEK или Systeme Electric.')
        for supplier_name, upload in [('IEK', iek_upload), ('Systeme Electric', system_upload)]:
            if upload is not None:
                try:
                    datasets[supplier_name] = load_dataset(upload, supplier_name)
                except (ValueError, KeyError, IndexError, OSError, zipfile.BadZipFile, sqlite3.Error) as error:
                    source_errors.append(f'{supplier_name}: {error}')
        st.html('<hr class="ekt-input-divider"><div class="ekt-panel-title"><span>02</span><h2>Параметры расчёта</h2></div>')
        earliest = max((data.as_of + timedelta(days=1) for _, data in datasets.values()), default=date.today())
        tomorrow = date.today() + timedelta(days=1)
        default_date = max(date.fromisoformat(preferences.get('forecast_start', tomorrow.isoformat())), earliest)
        if 'forecast_start' in st.session_state and st.session_state.forecast_start < earliest:
            st.session_state.forecast_start = earliest
        forecast_start = st.date_input('Начало прогноза', value=default_date, min_value=earliest,
                                        key='forecast_start', format='DD.MM.YYYY',
                                        help='Прогноз начинается после последнего дня продаж в выбранных архивах.')
        lead_iek = st.number_input('Поставка IEK, дней', 1, 365, int(preferences.get('lead_iek', 30)), key='lead_iek')
        lead_system = st.number_input('Поставка Systeme Electric, дней', 1, 365,
                                      int(preferences.get('lead_system', 30)), key='lead_system')
        with st.expander('Запас и прирост спроса'):
            review = st.number_input('Интервал между расчётами, дней', 1, 90,
                                      int(preferences.get('review', 7)), key='review')
            safety = st.number_input('Страховой запас, дней спроса', 0, 90,
                                      int(preferences.get('safety', 7)), key='safety')
            growth = st.number_input('Дополнительный прирост спроса, %', -50, 200,
                                      int(preferences.get('growth', 0)), key='growth')
        st.caption('Сроки 30 дней заданы для демонстрации. Укажите реальные сроки поставки.')
        run = st.button('Рассчитать заказ', type='primary', width='stretch')
        st.caption('Данные, настройки и расчёты сохраняются на этом компьютере.')

sources = {key: upload.source_id if upload else None
           for key, upload in [('iek', iek_upload), ('system', system_upload),
                               ('balance', balance_upload), ('stockout', stockout_upload)]}
params = {'lead_iek': int(lead_iek), 'lead_system': int(lead_system), 'review': int(review),
          'safety': int(safety), 'growth': int(growth), 'forecast_start': forecast_start.isoformat()}
signature = tuple(sources.values()) + tuple(params.values())
current_preferences = {**preferences, **params, 'sources': sources,
                       'last_run_id': st.session_state.get('order_run_id', preferences.get('last_run_id'))}
if current_preferences != preferences:
    store.save_preferences(current_preferences)

with workspace:
    for error in source_errors:
        st.error('Не удалось обработать архив: ' + error)
    if run:
        if not iek_upload and not system_upload:
            st.warning('Для расчёта загрузите хотя бы один ZIP-архив слева.')
        elif not source_errors:
            try:
                with st.spinner('Анализируем продажи и сохраняем прогноз…'):
                    result = calculate([('IEK', iek_upload, lead_iek),
                                        ('Systeme Electric', system_upload, lead_system)],
                                       balance_upload, stockout_upload, review, safety, growth,
                                       forecast_start, datasets)
                    metadata = {
                        'title': f'План закупок · {forecast_start:%d.%m.%Y}',
                        'forecast_start': forecast_start.isoformat(),
                        'model_version': MODEL_VERSION,
                        'prospective': forecast_start > date.today(),
                        'created_on': date.today().isoformat(),
                        'params': params, 'sources': sources,
                        'dataset_ids': [dataset_id for dataset_id, _ in datasets.values()],
                        'observed_through': {name: data.as_of.isoformat() for name, (_, data) in datasets.items()},
                        'signature': list(signature),
                    }
                    run_id = store.save_run(result, metadata)
                st.session_state.order_result = result
                st.session_state.order_run_id = run_id
                st.session_state.order_signature = signature
                st.session_state.order_quantities = {}
                st.session_state.visible_order_ids = None
                st.session_state.pop('order_save_error', None)
                current_preferences['last_run_id'] = run_id
                store.save_preferences(current_preferences)
            except (ValueError, KeyError, IndexError, OSError, zipfile.BadZipFile, sqlite3.Error, pd.errors.ParserError) as error:
                st.error(f'Не удалось рассчитать и сохранить прогноз: {error}')
    if 'order_result' in st.session_state and st.session_state.order_signature == signature:
        render_results(st.session_state.order_result)
    else:
        if 'order_result' in st.session_state:
            st.info('Данные или параметры изменились. Нажмите «Рассчитать заказ», чтобы сохранить новый прогноз. '
                    'Предыдущий расчёт остаётся в истории BI-аналитики.')
        render_empty()
    render_methodology()

render_footer()
