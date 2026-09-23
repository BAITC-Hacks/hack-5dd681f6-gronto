"""Sales history and auditable forecast quality in the procurement workspace."""

from __future__ import annotations

import csv
import calendar
import hashlib
import io
import math
import sqlite3
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from procurement.analytics import evaluate_forecast, sales_history, summary_metrics


def _number(value, *, percent=False, signed=False):
    if value is None or pd.isna(value) or not math.isfinite(float(value)):
        return '—'
    text = f'{float(value):+,.1f}' if signed else f'{float(value):,.1f}'
    return text.replace(',', ' ').replace('.', ',') + (' %' if percent else '')


def parse_actuals_csv(content: bytes, supplier: str, start: date, end: date) -> pd.DataFrame:
    """Validate a complete supplier report while preserving literal product codes."""
    if start > end:
        raise ValueError('Начало отчётного периода должно быть не позже окончания.')
    if end > date.today():
        raise ValueError('Завершённый отчёт не может включать будущие даты.')
    if len(content) > 40_000_000:
        raise ValueError('CSV больше допустимых 40 МБ.')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Сохраните CSV в кодировке UTF-8.') from exc
    try:
        separator = csv.Sniffer().sniff(text[:8192], delimiters=',;\t').delimiter
        frame = pd.read_csv(io.StringIO(text), sep=separator, dtype=str, keep_default_na=False)
    except (csv.Error, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise ValueError('Не удалось прочитать CSV. Используйте шаблон с разделителем «;».') from exc
    required = ['supplier', 'code', 'date', 'qty']
    frame.columns = frame.columns.str.strip()
    if not set(required).issubset(frame.columns):
        raise ValueError('Требуются колонки supplier, code, date, qty.')
    frame = frame[required].copy()
    for name in ['supplier', 'code', 'date', 'qty']:
        frame[name] = frame[name].str.strip()
    if (frame.supplier != supplier).any():
        raise ValueError(f'Все строки CSV должны относиться к поставщику «{supplier}». '
                         'Загрузите отдельный отчёт для каждого поставщика.')
    if frame.code.eq('').any():
        raise ValueError('У каждой строки должен быть непустой код товара в колонке code.')
    if not frame.date.str.fullmatch(r'\d{4}-\d{2}-\d{2}').all():
        raise ValueError('Даты в колонке date должны иметь формат ГГГГ-ММ-ДД.')
    try:
        frame['date'] = pd.to_datetime(frame.date, format='%Y-%m-%d', errors='raise').dt.date
        frame['qty'] = pd.to_numeric(frame.qty.str.replace(' ', '').str.replace(',', '.'), errors='raise')
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Проверьте даты и числа в колонках date и qty.') from exc
    if not frame.qty.map(lambda value: math.isfinite(float(value)) and float(value) >= 0).all():
        raise ValueError('Количество qty должно быть конечным неотрицательным числом. '
                         'Возвраты не включайте в отчёт валовых продаж.')
    if not frame.empty and ((frame.date < start) | (frame.date > end)).any():
        raise ValueError('Все даты продаж должны попадать в указанный отчётный период.')
    if not frame.empty and not math.isfinite(float(frame.qty.sum())):
        raise ValueError('Суммарное количество слишком велико. Проверьте колонку qty.')
    return frame


def _csv_download(label, frame, filename, key):
    st.download_button(label, frame.to_csv(index=False, sep=';').encode('utf-8-sig'),
                       file_name=filename, mime='text/csv', key=key, on_click='ignore')


def _run_label(run):
    metadata = run.get('metadata', {})
    stamp = str(run.get('created_at', '')).replace('T', ' ')[:19]
    title = metadata.get('title') or f'Расчёт № {run["id"]}'
    return f'№ {run["id"]} · {stamp} UTC · {title}'


def _period_caption(rows):
    if rows.empty or not {'Начало прогноза', 'Конец прогноза'}.issubset(rows.columns):
        return 'Горизонт прогноза не указан'
    start = pd.to_datetime(rows['Начало прогноза'], errors='coerce').min()
    end = pd.to_datetime(rows['Конец прогноза'], errors='coerce').max()
    if pd.isna(start) or pd.isna(end):
        return 'Горизонт прогноза не указан'
    return f'{start.date():%d.%m.%Y} — {end.date() - timedelta(days=1):%d.%m.%Y} включительно'


def _render_overview(store, datasets):
    st.subheader('Динамика продаж')
    st.caption('Количество проданных единиц по последнему сохранённому архиву каждого поставщика. '
               'Повторные загрузки одного поставщика не суммируются. Денежные показатели не рассчитываются.')
    if datasets.empty:
        st.info('Сохраните первый расчёт в разделе «План закупок» — здесь появится история продаж.')
        return
    latest = datasets.sort_values('imported_at', kind='stable').drop_duplicates('supplier', keep='last')
    loaded = [store.get_dataset(item) for item in latest.id]
    history = sales_history(loaded)
    if history.empty:
        st.info('В сохранённых архивах нет помесячной истории продаж.')
        return
    history['month'] = pd.to_datetime(history.month)
    history['category'] = history.category.fillna('Не задана').astype(str)
    left, right = st.columns(2)
    supplier = left.selectbox('Поставщик', ['Все поставщики', *sorted(history.supplier.unique())],
                              key='bi_sales_supplier')
    view = history if supplier == 'Все поставщики' else history[history.supplier == supplier]
    category = right.selectbox('Категория', ['Все категории', *sorted(view.category.unique())],
                               key='bi_sales_category')
    if category != 'Все категории':
        view = view[view.category == category]
    periods = sorted(history.month.unique())
    start_col, end_col = st.columns(2)
    start = start_col.selectbox('С месяца', periods, format_func=lambda item: pd.Timestamp(item).strftime('%m.%Y'),
                                key='bi_sales_start')
    end = end_col.selectbox('По месяц', periods, index=len(periods) - 1,
                            format_func=lambda item: pd.Timestamp(item).strftime('%m.%Y'), key='bi_sales_end')
    if start > end:
        st.warning('Начало периода должно быть не позже окончания.')
        return
    view = view[(view.month >= start) & (view.month <= end)]
    if view.empty:
        st.info('За выбранный период и по этим фильтрам данных нет.')
        return
    first, second, third = st.columns(3)
    first.metric('Продажи, шт.', _number(view.qty.sum()))
    second.metric('Месяцев с данными', str(view.month.nunique()))
    third.metric('Поставщиков', str(view.supplier.nunique()))
    partial = [data for data in loaded if data.name in set(view.supplier)
               and pd.Timestamp(start).date() <= data.as_of.replace(day=1) <= pd.Timestamp(end).date()
               and data.as_of.day < calendar.monthrange(data.as_of.year, data.as_of.month)[1]]
    if partial:
        last_operations = ' · '.join(f'{data.name}: {data.as_of:%d.%m.%Y}' for data in partial)
        st.caption('Последний месяц может быть неполным. Даты последних операций: ' + last_operations + '.')
    monthly = view.groupby(['month', 'supplier'], as_index=False).qty.sum().rename(
        columns={'month': 'Месяц', 'supplier': 'Поставщик', 'qty': 'Продажи, шт.'})
    st.line_chart(monthly, x='Месяц', y='Продажи, шт.', color='Поставщик', height=310)
    categories = view.groupby('category', as_index=False).qty.sum().sort_values('qty', ascending=False).head(15)
    if len(categories) > 1:
        st.caption('Категории с наибольшими продажами · до 15 категорий')
        st.bar_chart(categories.rename(columns={'category': 'Категория', 'qty': 'Продажи, шт.'}),
                     x='Категория', y='Продажи, шт.', horizontal=True, color='#2C7294', height=320)
    with st.expander('Помесячные данные и источники'):
        table = view.rename(columns={'month': 'Месяц', 'supplier': 'Поставщик',
                                     'category': 'Категория', 'qty': 'Продажи, шт.'}).copy()
        table['Месяц'] = table['Месяц'].dt.strftime('%Y-%m')
        st.dataframe(table, hide_index=True, width='stretch')
        _csv_download('Скачать продажи · CSV', table, 'sales_history.csv', 'bi_sales_csv')
        st.dataframe(latest[['supplier', 'as_of', 'imported_at']].rename(columns={
            'supplier': 'Поставщик', 'as_of': 'Последняя операция', 'imported_at': 'Архив сохранён'}),
            hide_index=True, width='stretch')


def _filtered_forecast(rows):
    left, right = st.columns(2)
    supplier = left.selectbox('Поставщик', ['Все поставщики', *sorted(rows['Поставщик'].dropna().unique())],
                              key='bi_accuracy_supplier')
    view = rows.copy() if supplier == 'Все поставщики' else rows[rows['Поставщик'] == supplier].copy()
    if 'Категория' not in view:
        view['Категория'] = 'Не задана'
    view['Категория'] = view['Категория'].fillna('Не задана').astype(str)
    category = right.selectbox('Категория', ['Все категории', *sorted(view['Категория'].unique())],
                               key='bi_accuracy_category')
    if category != 'Все категории':
        view = view[view['Категория'] == category]
    search = st.text_input('Поиск товара', placeholder='Код 1С, артикул или название', key='bi_accuracy_search')
    fields = [name for name in ['Код 1С', 'Артикул', 'Наименование'] if name in view]
    if search.strip() and fields:
        matches = view[fields].fillna('').astype(str).apply(
            lambda column: column.str.contains(search.strip(), case=False, regex=False))
        view = view[matches.any(axis=1)]
    return view


def _render_accuracy(store, runs):
    st.subheader('Точность прогноза')
    st.caption('Сравниваем сохранённый прогноз спроса с валовыми продажами за тот же период. '
               'Количество к заказу и ручные правки менеджера не меняют прогноз.')
    if not runs:
        st.info('Сохранённых прогнозов пока нет. Выполните расчёт в разделе «План закупок».')
        return
    show_historical = st.toggle('Показать также исторические сценарии', key='bi_include_historical',
                                 help='Для оценки прогноза на будущее расчёт должен быть сохранён '
                                      'до дня начала периода. Сценарии с началом в день расчёта '
                                      'или раньше рассматриваются отдельно.')
    choices = [run for run in runs if show_historical or run.get('metadata', {}).get('prospective') is True]
    if not choices:
        st.info('Пока сохранены только исторические сценарии. Для контроля будущего прогноза '
                'сохраните расчёт до начала его периода. Исторические сценарии доступны по переключателю выше.')
        return
    lookup = {run['id']: run for run in choices}
    selected = st.selectbox('Сохранённый прогноз', list(lookup),
                            format_func=lambda value: _run_label(lookup[value]), key='bi_accuracy_run')
    run = lookup[selected]
    if run.get('metadata', {}).get('prospective') is not True:
        st.warning('Исторический сценарий: период начинается в день сохранения расчёта или раньше '
                   'либо дата создания не подтверждена. Его показатели не служат оценкой прогноза на будущее.')
    rows = run['result']['rows']
    st.caption('Период: ' + _period_caption(rows) + '. Горизонт может различаться по поставщикам.')
    if rows.empty:
        st.info('В этом расчёте нет позиций для оценки.')
        return
    if not {'Начало прогноза', 'Конец прогноза'}.issubset(rows.columns):
        st.info('В старом расчёте не сохранены границы прогноза. Создайте новый расчёт для отслеживания точности.')
        return
    view = _filtered_forecast(rows)
    if view.empty:
        st.info('Нет товаров, соответствующих выбранным фильтрам.')
        return
    evaluated = evaluate_forecast(view, store.get_actuals(), store.get_coverage())
    metrics = summary_metrics(evaluated)
    matched = evaluated[evaluated['Статус'] == 'Оценён']
    first, second, third, fourth = st.columns(4)
    first.metric('Оценено позиций', f'{metrics["evaluated_count"]} / {len(evaluated)}')
    second.metric('MAE, шт.', _number(metrics.get('mae')),
                  help='Средняя абсолютная ошибка по оценённым позициям.')
    third.metric('WAPE', _number(metrics.get('wape'), percent=True),
                 help='Сумма абсолютных ошибок / сумма фактических продаж × 100 %. При нулевом факте не определена.')
    fourth.metric('Смещение', _number(metrics.get('bias'), percent=True, signed=True),
                  help='Сумма (прогноз − факт) / сумма факта × 100 %. Плюс означает завышенный прогноз.')
    if matched.empty:
        st.info('Точность пока не рассчитана: дождитесь окончания горизонта и загрузите полный отчёт '
                'продаж в разделе «Загрузить факт». Отсутствие отчёта не означает нулевые продажи.')
    else:
        pending = len(evaluated) - len(matched)
        if pending:
            st.info(f'Метрики рассчитаны по {len(matched)} позициям с полностью покрытым периодом. '
                    f'Остальные {pending} позиций ожидают окончания периода или полного отчёта.')
        st.caption(f'Только оценённые позиции: прогноз {_number(metrics.get("total_forecast"))} шт. · '
                   f'факт {_number(metrics.get("total_actual"))} шт. '
                   'Положительная ошибка означает завышение прогноза; отрицательная — занижение.')
        if metrics.get('wape') is None:
            st.caption('При нулевой сумме факта WAPE и процентное смещение не определены; '
                       'ошибка в штуках остаётся доступной.')
        st.write('**Прогноз и факт по товарам**')
        st.scatter_chart(matched, x='Прогноз', y='Факт', color='Поставщик',
                         x_label='Прогноз спроса, шт.', y_label='Фактические продажи, шт.', height=310)
        st.caption('Каждая точка — одна позиция выбранного расчёта. Совпадение координат означает точный прогноз.')
        largest = matched.nlargest(20, 'Абсолютная ошибка').copy()
        largest['Товар'] = largest['Поставщик'].astype(str) + ' · ' + largest['Код 1С'].astype(str)
        st.write('**Наибольшие отклонения · до 20 позиций**')
        st.bar_chart(largest, x='Товар', y='Отклонение', horizontal=True,
                     color='#2C7294', height=max(180, min(520, len(largest) * 30)))
    with st.expander('Как читать метрики'):
        st.write('MAE показывает среднюю ошибку в штуках. WAPE показывает общую абсолютную ошибку '
                 'в процентах от фактических продаж. Смещение показывает, завышен или занижен прогноз '
                 'в целом. Меньшие MAE и WAPE означают более точный прогноз; смещение желательно близкое к нулю.')
        st.write('Метрики относятся к выбранному расчёту и фильтрам. Неполные периоды исключаются. '
                 'Для товара с нулевым фактом процентная ошибка не определена. '
                 'Продажи могут быть ниже спроса из-за отсутствия товара: это сравнение с продажами, '
                 'а не измерение неудовлетворённого спроса.')
    columns = [column for column in ['Поставщик', 'Код 1С', 'Наименование', 'Категория', 'Статус',
                                     'Начало прогноза', 'Конец прогноза', 'Прогноз', 'Факт',
                                     'Отклонение', 'Абсолютная ошибка', 'Ошибка, %'] if column in evaluated]
    table = evaluated[columns].copy()
    table['Конец прогноза'] = pd.to_datetime(table['Конец прогноза']).map(
        lambda value: (value.date() - timedelta(days=1)).isoformat())
    table = table.rename(columns={'Конец прогноза': 'По дату включительно'})
    st.dataframe(table, hide_index=True, width='stretch', column_config={
        'Прогноз': st.column_config.NumberColumn(format='%.1f'),
        'Факт': st.column_config.NumberColumn(format='%.1f'),
        'Ошибка, %': st.column_config.NumberColumn(format='%.1f'),
    })
    _csv_download('Скачать сравнение · CSV', table, f'forecast_accuracy_{selected}.csv', 'bi_accuracy_csv')


def _render_import(store, datasets):
    st.subheader('Фактические продажи')
    st.write('Загрузите полный отчёт одного поставщика за указанный период. '
             'Границы периода включаются в отчёт. Для проверки используется код товара 1С.')
    st.caption('Формат CSV: supplier;code;date;qty. Даты — ГГГГ-ММ-ДД, количество — '
               'валовые продажи в штуках без вычета возвратов. Повторные строки товара за день суммируются.')
    st.download_button('Скачать шаблон CSV', 'supplier;code;date;qty\n'.encode('utf-8-sig'),
                       file_name='actual_sales_template.csv', mime='text/csv', key='bi_actual_template', on_click='ignore')
    with st.expander('Пример заполнения'):
        st.code('supplier;code;date;qty\nIEK;001234;2026-09-01;12\nIEK;001234;2026-09-02;4', language='text')
        st.write('Если за весь период продаж не было, загрузите CSV только с заголовками. '
                 'После подтверждения полноты это будет отчёт с нулевыми продажами по всем товарам поставщика.')
    suppliers = sorted(set(['IEK', 'Systeme Electric']) | (set(datasets.supplier) if not datasets.empty else set()))
    supplier = st.selectbox('Поставщик отчёта', suppliers, key='bi_actual_supplier')
    yesterday = date.today() - timedelta(days=1)
    first, second = st.columns(2)
    start = first.date_input('Отчёт с', value=yesterday.replace(day=1), max_value=date.today(), key='bi_actual_start')
    end = second.date_input('Отчёт по включительно', value=yesterday, max_value=date.today(), key='bi_actual_end')
    uploaded = st.file_uploader('Полный отчёт продаж · CSV', type='csv', key='bi_actual_file')
    if uploaded is None:
        st.info('Выберите файл и укажите период, который он полностью покрывает.')
        return
    content = uploaded.getvalue()
    try:
        frame = parse_actuals_csv(content, supplier, start, end)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.caption(f'Проверено: {len(frame)} строк · {frame.code.nunique()} товаров · '
               f'{_number(frame.qty.sum())} проданных единиц.')
    if not frame.empty:
        st.dataframe(frame.head(10), hide_index=True, width='stretch')
    else:
        st.warning('Файл содержит только заголовки. После сохранения весь выбранный период будет '
                   'подтверждён как период без продаж по всем товарам этого поставщика.')
    existing = store.get_coverage(supplier=supplier)
    if not existing.empty:
        overlaps = existing[(pd.to_datetime(existing.start).dt.date <= end)
                            & (pd.to_datetime(existing.end).dt.date >= start)]
        if not overlaps.empty:
            st.warning('Этот период пересекается с сохранённым отчётом. Новый полный отчёт заменит '
                       'продажи поставщика внутри выбранных дат, включая обнуление отсутствующих товаров. '
                       'Исходные файлы и история импортов сохранятся.')
    confirmation_id = hashlib.sha256(content + f'{supplier}|{start}|{end}'.encode()).hexdigest()[:16]
    with st.form('bi_actual_confirm_form'):
        confirmed = st.checkbox('Подтверждаю: это полный отчёт по всем товарам выбранного поставщика '
                                 'за указанный период; отсутствие товара в CSV означает нулевые продажи.',
                                 key=f'bi_actual_confirm_{confirmation_id}')
        submitted = st.form_submit_button('Сохранить фактические продажи', type='primary')
    if not submitted:
        return
    if not confirmed:
        st.error('Подтвердите полноту отчёта перед сохранением. Без этого нельзя отличить нулевые продажи от отсутствующих данных.')
        return
    try:
        import_id = store.import_actuals(frame, supplier, start, end,
                                        filename=getattr(uploaded, 'name', 'actual_sales.csv'), content=content)
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        st.error(f'Отчёт не сохранён: {exc}')
        return
    st.session_state.bi_saved_notice = (f'Отчёт № {import_id} сохранён: {supplier}, '
                                       f'{start:%d.%m.%Y} — {end:%d.%m.%Y}. Метрики прогноза обновлены.')
    st.rerun()


def _render_history(store, runs, datasets, on_open_run):
    st.subheader('Сохранённые расчёты')
    st.caption('Каждый расчёт хранит исходный прогноз, параметры и связанные данные. '
               'Ручные количества заказа сохраняются отдельно от прогноза.')
    if not runs:
        st.info('Расчётов пока нет. Создайте первый прогноз в разделе «План закупок».')
    else:
        rows = []
        for run in runs:
            metadata = run.get('metadata', {})
            rows.append({'№': run['id'], 'Сохранён (UTC)': str(run.get('created_at', '')).replace('T', ' ')[:19],
                         'Название': metadata.get('title', ''), 'Период': _period_caption(run['result']['rows']),
                         'Позиций': len(run['result']['rows']), 'Модель': metadata.get('model_version', 'Не указана'),
                         'Тип': 'Прогноз на будущее' if metadata.get('prospective') is True else 'Исторический сценарий'})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')
        lookup = {run['id']: run for run in runs}
        selected = st.selectbox('Открыть сохранённый расчёт', list(lookup),
                                format_func=lambda value: _run_label(lookup[value]), key='bi_history_run')
        if on_open_run is not None:
            st.button('Перейти к расчёту', on_click=on_open_run, args=(selected,), key='bi_open_run', type='primary')
        run = lookup[selected]
        metadata = run.get('metadata', {})
        edits = run.get('edits', pd.DataFrame())
        st.caption(f'Сохранено ручных корректировок: {len(edits)}.')
        with st.expander('Параметры и происхождение расчёта'):
            st.write('Версия модели: ' + str(metadata.get('model_version', 'Не указана')))
            params = metadata.get('params', {})
            if params:
                labels = {'review': 'Период между заказами, дней', 'review_days': 'Период между заказами, дней',
                          'safety': 'Страховой запас, дней', 'safety_days': 'Страховой запас, дней',
                          'growth': 'Внешний рост, %', 'lead_iek': 'Поставка IEK, дней',
                          'lead_system': 'Поставка Systeme Electric, дней', 'forecast_start': 'Начало прогноза'}
                st.dataframe(pd.DataFrame([{'Параметр': labels.get(key, key), 'Значение': str(value)}
                                           for key, value in params.items()]), hide_index=True, width='stretch')
            dataset_ids = metadata.get('dataset_ids', [])
            if dataset_ids and not datasets.empty:
                own = datasets[datasets.id.isin(dataset_ids)].rename(columns={
                    'id': 'Архив №', 'source_id': 'Источник №', 'supplier': 'Поставщик',
                    'as_of': 'Последняя операция', 'imported_at': 'Архив сохранён'})
                st.dataframe(own, hide_index=True, width='stretch')
            if metadata.get('sources'):
                sources = store.list_sources()
                source_ids = [value for value in metadata['sources'].values() if value]
                linked = sources[sources.id.isin(source_ids)]
                st.write('Связанные исходные файлы:')
                st.dataframe(linked[['filename', 'supplier', 'uploaded_at']].rename(columns={
                    'filename': 'Файл', 'supplier': 'Поставщик', 'uploaded_at': 'Сохранён'}),
                    hide_index=True, width='stretch')
            for issue in run['result'].get('issues', []):
                st.caption(str(issue))
    st.subheader('История загрузок факта')
    imports = store.list_actual_imports()
    if imports.empty:
        st.caption('Отчёты фактических продаж ещё не загружены.')
    else:
        st.dataframe(imports.rename(columns={'id': 'Импорт №', 'supplier': 'Поставщик', 'start': 'С даты',
                                             'end': 'По дату включительно', 'filename': 'Файл',
                                             'row_count': 'Строк', 'imported_at': 'Сохранён'}),
                     hide_index=True, width='stretch')
    with st.expander('Все исходные файлы'):
        sources = store.list_sources()
        if sources.empty:
            st.caption('Исходных файлов пока нет.')
        else:
            sources = sources.copy()
            sources['kind'] = sources.kind.replace({'archive': 'Архив поставщика', 'balances': 'Остатки',
                                                    'stockouts': 'Отсутствие товара', 'actuals': 'Продажи'})
            st.dataframe(sources.rename(columns={'id': 'Источник №', 'kind': 'Тип', 'supplier': 'Поставщик',
                                                 'filename': 'Файл', 'uploaded_at': 'Сохранён', 'size': 'Размер, байт'}),
                         hide_index=True, width='stretch')


def render_bi(store, on_open_run=None):
    """Render the persistent BI workspace using the storage/analytics contracts."""
    notice = st.session_state.pop('bi_saved_notice', None)
    if notice:
        st.success(notice)
    datasets = store.list_datasets()
    run_index = store.list_runs()
    runs = [] if run_index.empty else [store.get_run(item) for item in run_index.sort_values(
        'created_at', ascending=False, kind='stable').id]
    overview, accuracy, actuals, history = st.tabs(['Обзор продаж', 'Точность прогноза', 'Загрузить факт', 'История'])
    with overview:
        _render_overview(store, datasets)
    with accuracy:
        _render_accuracy(store, runs)
    with actuals:
        _render_import(store, datasets)
    with history:
        _render_history(store, runs, datasets, on_open_run)
