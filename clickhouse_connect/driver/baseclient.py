from abc import ABCMeta, abstractmethod
from typing import Iterable, Tuple, Optional, Any, Union, NamedTuple, Sequence, Dict

from clickhouse_connect.datatypes.registry import get_from_name
from clickhouse_connect.datatypes.base import ClickHouseType
from clickhouse_connect.driver.exceptions import ProgrammingError, InternalError
from clickhouse_connect.driver.query import QueryResult, np_result, to_pandas_df, from_pandas_df, escape_query_value


class ColumnDef(NamedTuple):
    name: str
    type: str
    default_type: str
    default_expression: str
    comment: str
    codec_expression: str
    ttl_expression: str

    @property
    def ch_type(self):
        return get_from_name(self.type)


class TableDef(NamedTuple):
    database: str
    name: str
    column_defs: Tuple[ColumnDef]
    engine: str
    order_by: str
    sort_by: str
    comment: str

    @property
    def column_names(self):
        return (t.name for t in self.column_defs)

    def column_types(self):
        return (t.type for t in self.column_defs)


class BaseClient(metaclass=ABCMeta):
    column_inserts = False

    def __init__(self, database: str, query_limit: int, uri: str):
        self.server_version, self.server_tz, self.database = \
            tuple(self.command('SELECT version(), timezone(), database()', use_database=False))
        if database and not database == '__default__':
            self.database = database
        self.limit = query_limit
        self.uri = uri

    def query(self, query: str, parameters=None, use_none: bool = True, settings=None) -> QueryResult:
        if parameters:
            escaped = {k: escape_query_value(v, self.server_tz) for k, v in parameters.items()}
            query %= escaped
        query = query.replace('\n', ' ')
        if self.limit and ' LIMIT ' not in query.upper() and 'SELECT ' in query.upper():
            query += f' LIMIT {self.limit}'
        return self.exec_query(query, use_none, settings)

    def query_np(self, query: str, parameters=None, settings: Optional[Dict] = None):
        return np_result(self.query(query, parameters=parameters, use_none=False, settings=settings))

    def query_df(self, query: str, parameters=None, settings: Optional[Dict] = None):
        return to_pandas_df(self.query(query, parameters=parameters, use_none=False, settings=settings))

    def insert_df(self, table: str, data_frame):
        insert = from_pandas_df(data_frame)
        return self.insert(table, **insert)

    @abstractmethod
    def exec_query(self, query: str, use_none: bool = True, settings: Optional[Dict] = None) -> QueryResult:
        pass

    def command(self, cmd: str, parameters=None, use_database: bool = True, settings: Optional[Dict] = None) \
            -> Union[str, int, Sequence[str]]:
        if parameters:
            escaped = {k: escape_query_value(v, self.server_tz) for k, v in parameters.items()}
            cmd %= escaped
        return self.exec_command(cmd, use_database, settings)

    @abstractmethod
    def exec_command(self, cmd, use_database: bool = True, settings: Optional[Dict] = None) -> Union[str, int, Sequence[str]]:
        pass

    @abstractmethod
    def ping(self):
        pass

    # pylint: disable=too-many-arguments
    def insert(self, table: str, data: Iterable[Iterable[Any]], column_names: Union[str or Iterable[str]] = '*',
               database: str = '', column_types: Optional[Iterable[ClickHouseType]] = None,
               column_type_names: Optional[Iterable[str]] = None, column_oriented: bool = False, settings: Optional[Dict] = None):
        table, database, full_table = self.normalize_table(table, database)
        if isinstance(column_names, str):
            if column_names == '*':
                column_defs = [cd for cd in self.table_columns(table, database)
                               if cd.default_type not in ('ALIAS', 'MATERIALIZED')]
                column_names = [cd.name for cd in column_defs]
                column_types = [cd.ch_type for cd in column_defs]
            else:
                column_names = [column_names]
        elif len(column_names) == 0:
            raise ValueError('Column names must be specified for insert')
        if column_types is None:
            if column_type_names:
                column_types = [get_from_name(name) for name in column_type_names]
            else:
                column_map: dict[str: ColumnDef] = {d.name: d for d in self.table_columns(table, database)}
                try:
                    column_types = [column_map[name].ch_type for name in column_names]
                except KeyError as ex:
                    raise ProgrammingError(f'Unrecognized column {ex} in table {table}') from None
        assert len(column_names) == len(column_types)
        self.data_insert(full_table, column_names, data, column_types, settings, column_oriented)

    def normalize_table(self, table: str, database: str) -> Tuple[str, str, str]:
        split = table.split('.')
        if len(split) > 1:
            full_name = table
            database = split[0]
            table = split[1]
        else:
            name = table
            database = database or self.database
            full_name = f'{database}.{name}'
        return table, database, full_name

    def table_columns(self, table: str, database: str) -> Tuple[ColumnDef]:
        column_result = self.query(f'DESCRIBE TABLE {database}.{table}')
        if not column_result.result_set:
            raise InternalError(f'No table columns found for {database}.{table}')
        return tuple(ColumnDef(**row) for row in column_result.named_results())

    @abstractmethod
    def data_insert(self, table: str, column_names: Iterable[str], data: Iterable[Iterable[Any]],
                    column_types: Iterable[ClickHouseType], settings: Optional[Dict] = None,
                    column_oriented: bool = False):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.close()
