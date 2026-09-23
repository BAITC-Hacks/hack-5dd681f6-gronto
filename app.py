"""Local browser UI: streamlit run app.py."""

from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from procurement.calculate import Settings, recommend
from procurement.ingest import load_archive


st.set_page_config(page_title='План закупок', page_icon='📦', layout='wide')
st.markdown('''<style>
.block-container {max-width: 1320px; padding-top: 2rem}
h1, h2, h3 {letter-spacing: -.025em}
[data-testid="stMetric"] {border: 1px solid #e7ebf0; border-radius: 12px; padding: 16px}
</style>''', unsafe_allow_html=True)

st.title('План закупок')
st.caption('Локальный MVP · проект рекомендаций для менеджера · без автоматической отправки поставщикам')


def read_optional(uploaded, required, label):
    if uploaded is None:
        return pd.DataFrame()
    frame = pd.read_csv(io.BytesIO(uploaded.getvalue()), sep=None, engine='python', encoding='utf-8-sig', dtype={'code':str})
    if not set(required).issubset(frame.columns):
        raise ValueError(f'{label}: требуются колонки {", ".join(required)}')
    frame['code'] = frame['code'].str.strip()
    return frame


@st.cache_data(show_spinner=False)
def parse_archive(content: bytes, name: str):
    return load_archive(content, name)


with st.sidebar:
    st.header('1. Данные')
    iek_upload = st.file_uploader('IEK.zip', type='zip', key='iek')
    system_upload = st.file_uploader('Systeme electric.zip', type='zip', key='system')
    st.caption('Архивы обрабатываются локально на этом компьютере. Пустые исходные файлы не сохраняются в базу данных.')
    with st.expander('Дополнительные данные'):
        balance_upload = st.file_uploader('Актуальные остатки · CSV', type='csv')
        stockout_upload = st.file_uploader('Периоды stockout · CSV', type='csv')
        st.caption('Остатки: supplier,code,balance. Stockout: supplier,code,start,end; даты ГГГГ-ММ-ДД.')
    st.header('2. Параметры')
    lead_iek = st.number_input('Срок новой поставки IEK, дней', 1, 365, 30)
    lead_system = st.number_input('Срок новой поставки Systeme Electric, дней', 1, 365, 30)
    review = st.number_input('Интервал между расчётами, дней', 1, 90, 7)
    safety = st.number_input('Страховой запас, дней спроса', 0, 90, 7)
    growth = st.number_input('Дополнительный прирост спроса, %', -50, 200, 0)
    st.caption('Сроки 30 дней заданы для демонстрации: подтвердите реальные сроки до заказа.')
    run = st.button('Рассчитать', type='primary', use_container_width=True)

if not run:
    st.info('Загрузите один или оба архива в боковой панели и нажмите «Рассчитать».')
    st.markdown('''### Что покажет сайт
Прогноз учитывает сезонность и устойчивый рост, исключает редкие крупные продажи,
вычитает поставки, которые успеют прибыть в расчётный период, и округляет количество
по кратности поставщика. Каждая строка содержит числа, из которых получена рекомендация.

**Ограничения текущих выгрузок.** Нет ID клиента и журнала stockout. Остаток IEK
относится к началу сентября; остаток Systeme Electric взят из сводной таблицы.
Актуальные остатки и периоды stockout можно добавить CSV-файлами.''')
    st.stop()

if not iek_upload and not system_upload:
    st.warning('Для расчёта загрузите хотя бы один ZIP-архив.')
    st.stop()

try:
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
    for name, upload, lead in [('IEK', iek_upload, lead_iek), ('Systeme Electric', system_upload, lead_system)]:
        if upload is None:
            continue
        with st.spinner(f'Обработка {name}…'):
            dataset = parse_archive(upload.getvalue(), name)
            settings = Settings(int(lead), int(review), int(safety), growth / 100)
            own_balances = balances[balances.supplier == name] if not balances.empty else balances
            own_stockouts = stockouts[stockouts.supplier == name] if not stockouts.empty else stockouts
            result = recommend(dataset, settings, own_stockouts, own_balances)
        results.append(result)
        issues.extend(dataset.issues)
        dates.append(f'{name}: {dataset.as_of.isoformat()}')
except (ValueError, KeyError, IndexError, OSError, pd.errors.ParserError) as error:
    st.error(f'Не удалось обработать данные: {error}')
    st.stop()

all_rows = pd.concat(results, ignore_index=True) if results else pd.DataFrame()
if all_rows.empty:
    st.warning('Нет позиций с известным остатком. Загрузите актуальные остатки CSV.')
    st.stop()

st.caption('Даты последних операций: ' + ' · '.join(dates))
demo = all_rows['Источник остатка'].str.contains('демонстрацион|сводной', case=False, regex=True).any()
if demo:
    st.warning('Демонстрационный расчёт: часть остатков не подтверждена на дату запуска. Перед утверждением заказа загрузите актуальные остатки.')
for warning in dict.fromkeys(issues):
    st.caption('• ' + warning)

with st.expander('Как получено количество'):
    st.write('Прогноз до следующего пополнения + страховой запас − остаток − товар, который прибудет вовремя. Результат округляется вверх до кратности. Крупные разовые операции уменьшаются до обычного размера продажи; дни отсутствия товара компенсируются только при загрузке журнала stockout. Новые заказы не отправляются автоматически.')

left, middle, right = st.columns(3)
left.metric('Позиций в расчёте', f'{len(all_rows):,}'.replace(',', ' '))
middle.metric('Позиций к заказу', f'{(all_rows["Рекомендовано"] > 0).sum():,}'.replace(',', ' '))
right.metric('Высокий риск дефицита', f'{((all_rows["Срочность"] == "Высокая") & (all_rows["Рекомендовано"] > 0)).sum():,}'.replace(',', ' '))

supplier = st.selectbox('Поставщик', ['Все', *sorted(all_rows['Поставщик'].unique())])
only_orders = st.toggle('Только позиции к заказу', value=True)
view = all_rows.copy()
if supplier != 'Все':
    view = view[view['Поставщик'] == supplier]
if only_orders:
    view = view[view['Рекомендовано'] > 0]
st.subheader('Проект заказа')
st.caption('Можно изменить количество в колонке «Рекомендовано» перед скачиванием. Изменение вручную не пересчитывает прогноз.')
edited = st.data_editor(view, hide_index=True, use_container_width=True, num_rows='fixed',
                        disabled=[c for c in view.columns if c != 'Рекомендовано'],
                        column_config={'Рекомендовано': st.column_config.NumberColumn(min_value=0, step=1, required=True)})
if not edited.empty:
    wrong = edited.apply(lambda r: int(r['Рекомендовано']) % int(r['Кратность']) != 0, axis=1)
    if wrong.any():
        st.error(f'{wrong.sum()} скорректированных количеств не соответствуют кратности. Исправьте их перед экспортом.')
    else:
        st.download_button('Скачать проект заказа CSV', edited.to_csv(index=False, sep=';').encode('utf-8-sig'),
                           file_name='zakazy_postavshchikam.csv', mime='text/csv')
