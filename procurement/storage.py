"""Local, transactional history of source data, forecasts and reported sales.

Forecast snapshots are immutable. Order edits and the latest reported actuals live
in separate tables so that correcting either cannot rewrite an old forecast.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from procurement.ingest import SupplierData


SCHEMA_VERSION = 1
DEFAULT_PATH = Path(__file__).resolve().parents[1] / 'data' / 'procurement.sqlite3'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _encode(value: Any) -> Any:
    """Tag non-JSON values explicitly; never execute code while reading a snapshot."""
    if isinstance(value, pd.DataFrame):
        return {'__type__': 'frame', 'columns': _encode(list(value.columns)),
                'index': _encode(list(value.index)), 'index_name': _encode(value.index.name),
                'index_dtype': str(value.index.dtype),
                'dtypes': [str(dtype) for dtype in value.dtypes],
                'data': [[_encode(cell) for cell in row] for row in value.itertuples(index=False, name=None)]}
    if value is pd.NA or value is pd.NaT:
        return {'__type__': 'missing'}
    if isinstance(value, datetime):
        return {'__type__': 'datetime', 'value': value.isoformat()}
    if isinstance(value, date):
        return {'__type__': 'date', 'value': value.isoformat()}
    if isinstance(value, dict):
        # A wrapper prevents user metadata keys from colliding with type tags.
        return {'__type__': 'dict', 'items': {str(key): _encode(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if hasattr(value, 'item'):
        return _encode(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return {'__type__': 'float', 'value': str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'Неподдерживаемый тип данных: {type(value).__name__}')


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value['__type__']
    if kind == 'dict':
        return {key: _decode(item) for key, item in value['items'].items()}
    if kind == 'date':
        return date.fromisoformat(value['value'])
    if kind == 'datetime':
        return datetime.fromisoformat(value['value'])
    if kind == 'missing':
        return pd.NA
    if kind == 'float':
        return float(value['value'])
    if kind == 'frame':
        frame = pd.DataFrame(_decode(value['data']), columns=_decode(value['columns']),
                             index=pd.Index(_decode(value['index']), dtype=value.get('index_dtype')))
        frame.index.name = _decode(value['index_name'])
        for column, dtype in zip(frame.columns, value['dtypes']):
            frame[column] = frame[column].astype(dtype)
        return frame
    raise ValueError('Неизвестный формат сохранённых данных')


def _pack(value: Any) -> bytes:
    content = json.dumps(_encode(value), ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return gzip.compress(content, mtime=0)


def _unpack(content: bytes) -> Any:
    return _decode(json.loads(gzip.decompress(content).decode('utf-8')))


def _identifier(value: Any, label: str) -> str:
    if value is None or not pd.api.types.is_scalar(value) or pd.isna(value):
        raise ValueError(f'{label}: пустое значение недопустимо')
    result = str(value).strip()
    if not result or '\x00' in result:
        raise ValueError(f'{label}: пустое или некорректное значение')
    return result


def _day(value: Any, label: str) -> date:
    if value is None or not pd.api.types.is_scalar(value) or pd.isna(value):
        raise ValueError(f'{label}: дата обязательна')
    # Numeric timestamps are usually accidental spreadsheet indexes.
    if isinstance(value, (int, float, bool)):
        raise ValueError(f'{label}: неверная дата')
    try:
        parsed = pd.Timestamp(value)
        if pd.isna(parsed):
            raise ValueError()
        return parsed.date()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f'{label}: неверная дата') from exc


class Store:
    """A file-backed store; every operation uses a short-lived connection."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get('PROCUREMENT_DB_PATH') or DEFAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError('База данных создана более новой версией приложения')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, supplier TEXT NOT NULL,
                    filename TEXT NOT NULL, uploaded_at TEXT NOT NULL,
                    size INTEGER NOT NULL, content BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS datasets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    supplier TEXT NOT NULL, as_of TEXT NOT NULL,
                    imported_at TEXT NOT NULL, digest TEXT NOT NULL, payload BLOB NOT NULL,
                    UNIQUE(source_id, digest)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    title TEXT NOT NULL, row_count INTEGER NOT NULL,
                    result BLOB NOT NULL, metadata BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_edits (
                    run_id INTEGER NOT NULL REFERENCES runs(id),
                    supplier TEXT NOT NULL, code TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity >= 0), updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, supplier, code)
                );
                CREATE TABLE IF NOT EXISTS actual_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplier TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                    filename TEXT NOT NULL, row_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id),
                    payload BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actuals (
                    supplier TEXT NOT NULL, code TEXT NOT NULL, date TEXT NOT NULL,
                    qty REAL NOT NULL CHECK(qty >= 0),
                    import_id INTEGER NOT NULL REFERENCES actual_imports(id),
                    PRIMARY KEY(supplier, code, date)
                );
                CREATE INDEX IF NOT EXISTS actuals_supplier_date ON actuals(supplier, date);
                CREATE INDEX IF NOT EXISTS imports_supplier_dates ON actual_imports(supplier, start, end);
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL
                );
            ''')
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=30000')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _source(connection, kind, supplier, filename, content) -> str:
        kind = _identifier(kind, 'Тип источника')
        supplier = _identifier(supplier, 'Поставщик')
        if not isinstance(content, bytes):
            raise ValueError('Содержимое файла должно быть bytes')
        scope = json.dumps([kind, supplier], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        source_id = hashlib.sha256(scope + b'\x00' + content).hexdigest()
        connection.execute('''INSERT OR IGNORE INTO sources
                              (id, kind, supplier, filename, uploaded_at, size, content)
                              VALUES (?, ?, ?, ?, ?, ?, ?)''',
                           (source_id, kind, supplier, str(filename), _now(), len(content), content))
        return source_id

    def save_source(self, kind: str, supplier: str, filename: str, content: bytes) -> str:
        with self._connection() as connection:
            return self._source(connection, kind, supplier, filename, content)

    def get_preferences(self) -> dict:
        with self._connection() as connection:
            row = connection.execute('SELECT payload FROM preferences WHERE id=1').fetchone()
        return _unpack(row['payload']) if row is not None else {}

    def save_preferences(self, preferences: dict) -> None:
        if not isinstance(preferences, dict):
            raise ValueError('Настройки должны быть словарём')
        payload = _pack(preferences)
        with self._connection() as connection:
            connection.execute('''INSERT INTO preferences(id,payload) VALUES (1,?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload''', (payload,))

    def list_sources(self, kind=None, supplier=None) -> pd.DataFrame:
        conditions, values = [], []
        for column, value in [('kind', kind), ('supplier', supplier)]:
            if value is not None:
                conditions.append(f'{column} = ?')
                values.append(value)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self._connection() as connection:
            return pd.read_sql_query('SELECT id,kind,supplier,filename,uploaded_at,size FROM sources' +
                                     where + ' ORDER BY uploaded_at DESC, id', connection, params=values)

    def get_source(self, source_id: str) -> dict:
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if row is None:
            raise KeyError(f'Источник {source_id} не найден')
        return dict(row)

    def save_dataset(self, source_id: str, data: SupplierData) -> int:
        payload = _pack(dict(name=data.name, monthly=data.monthly, events=data.events,
                             catalog=data.catalog, incoming=data.incoming,
                             as_of=data.as_of, issues=data.issues))
        digest = hashlib.sha256(payload).hexdigest()
        with self._connection() as connection:
            source = connection.execute('SELECT supplier FROM sources WHERE id=?', (source_id,)).fetchone()
            if source is None:
                raise KeyError(f'Источник {source_id} не найден')
            if source['supplier'] != data.name:
                raise ValueError('Поставщик набора данных не совпадает с источником')
            connection.execute('''INSERT OR IGNORE INTO datasets
                (source_id,supplier,as_of,imported_at,digest,payload) VALUES (?,?,?,?,?,?)''',
                (source_id, data.name, _day(data.as_of, 'Дата данных').isoformat(), _now(), digest, payload))
            return int(connection.execute('SELECT id FROM datasets WHERE source_id=? AND digest=?',
                                          (source_id, digest)).fetchone()['id'])

    def get_dataset(self, dataset_id: int) -> SupplierData:
        with self._connection() as connection:
            row = connection.execute('SELECT payload FROM datasets WHERE id=?', (int(dataset_id),)).fetchone()
        if row is None:
            raise KeyError(f'Набор данных {dataset_id} не найден')
        return SupplierData(**_unpack(row['payload']))

    def list_datasets(self) -> pd.DataFrame:
        with self._connection() as connection:
            frame = pd.read_sql_query('SELECT id,source_id,supplier,as_of,imported_at FROM datasets ORDER BY id DESC', connection)
        frame['as_of'] = frame['as_of'].map(date.fromisoformat)
        return frame

    def save_run(self, result: dict, metadata: dict) -> int:
        if not isinstance(result.get('rows'), pd.DataFrame):
            raise ValueError('Результат должен содержать таблицу rows')
        title = str(metadata.get('title') or 'Расчёт закупки')
        result_payload, metadata_payload = _pack(result), _pack(metadata)
        with self._connection() as connection:
            cursor = connection.execute('''INSERT INTO runs
                (created_at,title,row_count,result,metadata) VALUES (?,?,?,?,?)''',
                (_now(), title, len(result['rows']), result_payload, metadata_payload))
            return int(cursor.lastrowid)

    def list_runs(self) -> pd.DataFrame:
        with self._connection() as connection:
            return pd.read_sql_query('SELECT id,created_at,title,row_count FROM runs ORDER BY id DESC', connection)

    def get_run(self, run_id: int) -> dict:
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM runs WHERE id=?', (int(run_id),)).fetchone()
            if row is None:
                raise KeyError(f'Прогноз {run_id} не найден')
            edits = pd.read_sql_query('''SELECT supplier,code,quantity,updated_at FROM order_edits
                                       WHERE run_id=? ORDER BY supplier,code''', connection, params=(int(run_id),))
        return {'id': row['id'], 'created_at': row['created_at'], 'metadata': _unpack(row['metadata']),
                'result': _unpack(row['result']), 'edits': edits}

    def save_order_quantity(self, run_id: int, supplier: str, code: str, quantity) -> None:
        supplier, code = _identifier(supplier, 'Поставщик'), _identifier(code, 'Код товара')
        if pd.api.types.is_bool(quantity):
            raise ValueError('Количество должно быть целым неотрицательным числом')
        try:
            numeric = Decimal(str(quantity))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError('Количество должно быть целым неотрицательным числом') from exc
        if not numeric.is_finite() or numeric < 0 or numeric != numeric.to_integral_value() or numeric >= 2**63:
            raise ValueError('Количество должно быть целым неотрицательным числом')
        with self._connection() as connection:
            run = connection.execute('SELECT result FROM runs WHERE id=?', (int(run_id),)).fetchone()
            if run is None:
                raise KeyError(f'Прогноз {run_id} не найден')
            rows = _unpack(run['result'])['rows']
            supplier_column = 'Поставщик' if 'Поставщик' in rows else 'supplier'
            code_column = 'Код 1С' if 'Код 1С' in rows else 'code'
            if supplier_column not in rows or code_column not in rows or not (
                    (rows[supplier_column].map(str) == supplier) & (rows[code_column].map(str) == code)).any():
                raise ValueError('Товар этого поставщика отсутствует в сохранённом прогнозе')
            connection.execute('''INSERT INTO order_edits(run_id,supplier,code,quantity,updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(run_id,supplier,code)
                DO UPDATE SET quantity=excluded.quantity,updated_at=excluded.updated_at''',
                (int(run_id), supplier, code, int(numeric), _now()))

    def import_actuals(self, frame: pd.DataFrame, supplier: str, start, end,
                       filename: str = '', content: bytes = b'') -> int:
        supplier = _identifier(supplier, 'Поставщик')
        first, last = _day(start, 'Начало периода'), _day(end, 'Конец периода')
        if first > last:
            raise ValueError('Начало периода должно предшествовать окончанию')
        if not isinstance(frame, pd.DataFrame) or not {'code', 'date', 'qty'}.issubset(frame.columns):
            raise ValueError('Нужны столбцы code, date, qty')
        actuals = frame.loc[:, ['code', 'date', 'qty']].copy()
        actuals['code'] = actuals['code'].map(lambda item: _identifier(item, 'Код товара'))
        actuals['date'] = actuals['date'].map(lambda item: _day(item, 'Дата продажи'))
        try:
            if actuals['qty'].map(lambda value: isinstance(value, bool)).any():
                raise ValueError()
            actuals['qty'] = pd.to_numeric(actuals['qty'], errors='raise').astype(float)
        except (ValueError, TypeError) as exc:
            raise ValueError('Продажи должны быть неотрицательными числами') from exc
        if not actuals['qty'].map(math.isfinite).all() or (actuals['qty'] < 0).any():
            raise ValueError('Продажи должны быть конечными неотрицательными числами')
        if not actuals['date'].map(lambda value: first <= value <= last).all():
            raise ValueError('Дата продажи находится вне заявленного периода')
        actuals = actuals.groupby(['code', 'date'], as_index=False, sort=True)['qty'].sum()
        if not actuals['qty'].map(math.isfinite).all():
            raise ValueError('Сумма продаж слишком велика')
        payload = _pack(actuals)
        with self._connection() as connection:
            source_id = self._source(connection, 'actuals', supplier, filename, content)
            cursor = connection.execute('''INSERT INTO actual_imports
                (supplier,start,end,filename,row_count,imported_at,source_id,payload) VALUES (?,?,?,?,?,?,?,?)''',
                (supplier, first.isoformat(), last.isoformat(), str(filename), len(actuals), _now(), source_id, payload))
            import_id = int(cursor.lastrowid)
            # The report declares complete coverage, so absent SKU/days become zero.
            connection.execute('DELETE FROM actuals WHERE supplier=? AND date BETWEEN ? AND ?',
                               (supplier, first.isoformat(), last.isoformat()))
            connection.executemany('INSERT INTO actuals(supplier,code,date,qty,import_id) VALUES (?,?,?,?,?)',
                                   [(supplier, row.code, row.date.isoformat(), row.qty, import_id)
                                    for row in actuals.itertuples(index=False)])
        return import_id

    def get_actuals(self, supplier=None, start=None, end=None) -> pd.DataFrame:
        conditions, values = [], []
        if supplier is not None:
            conditions.append('supplier=?')
            values.append(supplier)
        for value, operator in [(start, '>='), (end, '<=')]:
            if value is not None:
                conditions.append(f'date {operator} ?')
                values.append(_day(value, 'Дата фильтра').isoformat())
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self._connection() as connection:
            frame = pd.read_sql_query('SELECT supplier,code,date,qty FROM actuals' + where +
                                     ' ORDER BY supplier,date,code', connection, params=values)
        frame['date'] = frame['date'].map(date.fromisoformat)
        return frame

    def get_coverage(self, supplier=None) -> pd.DataFrame:
        where, values = (' WHERE supplier=?', (supplier,)) if supplier is not None else ('', ())
        with self._connection() as connection:
            frame = pd.read_sql_query('SELECT supplier,start,end,id AS import_id FROM actual_imports' +
                                     where + ' ORDER BY id', connection, params=values)
        for column in ['start', 'end']:
            frame[column] = frame[column].map(date.fromisoformat)
        return frame

    def list_actual_imports(self) -> pd.DataFrame:
        with self._connection() as connection:
            frame = pd.read_sql_query('''SELECT id,supplier,start,end,filename,row_count,imported_at
                                       FROM actual_imports ORDER BY id DESC''', connection)
        for column in ['start', 'end']:
            frame[column] = frame[column].map(date.fromisoformat)
        return frame

    def get_actual_import(self, import_id: int) -> dict:
        """Retrieve provenance, including the original source and normalized report."""
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM actual_imports WHERE id=?', (int(import_id),)).fetchone()
        if row is None:
            raise KeyError(f'Загрузка факта {import_id} не найдена')
        result = dict(row)
        result['rows'] = _unpack(result.pop('payload'))
        result['start'], result['end'] = date.fromisoformat(result['start']), date.fromisoformat(result['end'])
        return result
