"""轻量结构补齐：create_all 不会给既有表加列，这里只补缺失列。

历史库（无精确检验列）在启动时补齐；不执行数据迁移，不改动已有列。
完整 DDL 见 migrations/。
"""
import logging
from sqlalchemy import inspect, text

# 表 -> [(列名, MySQL 类型, SQLite 类型)]
COLUMNS = {
    'mining_runs': [
        ('alpha', 'FLOAT', 'FLOAT'),
    ],
    'association_rules': [
        ('expected', 'FLOAT', 'FLOAT'),
        ('p_value', 'FLOAT', 'FLOAT'),
        ('q_value', 'FLOAT', 'FLOAT'),
        ('in_scope', 'BOOLEAN', 'BOOLEAN'),
        ('qualified', 'BOOLEAN', 'BOOLEAN'),
        ('reasons', 'JSON', 'JSON'),
        ('per_equipment', 'JSON', 'JSON'),
    ],
    'kits': [
        ('evidence', 'JSON', 'JSON'),
    ],
}


def upgrade_schema(engine):
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    sqlite = engine.dialect.name == 'sqlite'
    with engine.begin() as connection:
        for table, columns in COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {column['name'] for column in inspector.get_columns(table)}
            for name, mysql_type, sqlite_type in columns:
                if name in present:
                    continue
                kind = sqlite_type if sqlite else mysql_type
                logging.info('adding column %s.%s', table, name)
                connection.execute(
                    text('ALTER TABLE %s ADD COLUMN %s %s' % (table, name, kind))
                )
