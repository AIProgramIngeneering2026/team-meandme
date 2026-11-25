"""Подключение Alembic к хранилищу сервиса."""

from alembic import context

from meandme.storage import sqlite_engine

engine = sqlite_engine()
with engine.begin() as connection:
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
