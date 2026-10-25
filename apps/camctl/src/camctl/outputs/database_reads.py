"""有限SQL结果在业务worker中取完，游标不交给事件循环。"""
from contextlib import closing


async def read_one(database, query, parameters=()):
    def execute(owned):
        with closing(owned.connection.execute(query, parameters)) as cursor:
            return cursor.fetchone()
    return await database.read(execute)


async def read_all(database, query, parameters=()):
    def execute(owned):
        with closing(owned.connection.execute(query, parameters)) as cursor:
            return tuple(cursor.fetchall())
    return await database.read(execute)
