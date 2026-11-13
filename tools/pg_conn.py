#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ad-hoc PostgreSQL / Hologres query helper.

Security
--------
No host, account or password is stored here. Everything comes from the
environment (or a root ``.env``), matching ``.env.example``::

    CREDIT_AGENT_PG_HOST
    CREDIT_AGENT_PG_PORT       # defaults to 5432
    CREDIT_AGENT_PG_DATABASE
    CREDIT_AGENT_PG_USER
    CREDIT_AGENT_PG_PASSWORD

Importing this module never opens a connection; run it as a script to query.
"""
import os

import psycopg2
import pandas as pd

host = os.getenv("CREDIT_AGENT_PG_HOST", "")
db = os.getenv("CREDIT_AGENT_PG_DATABASE", "")
user = os.getenv("CREDIT_AGENT_PG_USER", "")
pwd = os.getenv("CREDIT_AGENT_PG_PASSWORD", "")
port = int(os.getenv("CREDIT_AGENT_PG_PORT") or 5432)


class Db_Holo:
    def __init__(self, host, db, user, pwd, port):
        self.host = host
        self.db = db
        self.user = user
        self.pwd = pwd
        self.port = port
        self._conn = self._connect()
        self._cursor = self._conn.cursor()

    def _connect(self):
        return psycopg2.connect(
            database=self.db,
            user=self.user,
            password=self.pwd,
            host=self.host,
            port=self.port)

    def select(self, sqlCode):
        self.common(sqlCode)
        col_names = []
        result = {}
        column_count = len(self._cursor.description)
        for i in range(column_count):
            desc = self._cursor.description[i]
            col_names.append(desc[0])
        data = self._cursor.fetchall()
        result['head'] = col_names
        result['data'] = data
        return result

    def close(self):
        self._cursor.close()
        self._conn.close()

    def common(self, sqlCode):
        try:
            self._cursor.execute(sqlCode)
        except Exception as e:
            print(e)
            self._conn.rollback()
            self._cursor.execute(sqlCode)
        self._conn.commit()

    def __del__(self):
        self.close()


class PostgreSql:
    def __init__(self):
        try:
            self.connectPool = pool.SimpleConnectionPool(4, 12, host=db_conn2['host'], port=db_conn2['port'],
                                                         user=db_conn2['user'], password=db_conn2['pwd'],
                                                         database=db_conn2['db'], keepalives=1,
                                                         keepalives_idle=10, keepalives_interval=10, keepalives_count=1)
            # logger.info('holo pool init')
        except Exception as e:
            print(e)

    def getConnect(self):
        conn = self.connectPool.getconn()
        for i in range(10):
            if not conn or (conn.closed==1):
                self.connectPool.putconn(conn,close=True)
                conn = self.connectPool.getconn()
            else:
                try:
                    conn.poll()
                    break
                except Exception as e:
                    self.connectPool.putconn(conn, close=True)
                    conn = self.connectPool.getconn()
                    # logger.error('holo connect error:%s' % MSG_FORMAT(e=e))
                    continue

        cursor = conn.cursor()
        if i > 0:
            logger.info('connectPool getconn counts:%s' %str(i+1))
        return conn, cursor

    def closeConnect(self, conn, cursor):
        cursor.close()
        self.connectPool.putconn(conn)

    def closeAll(self):
        self.connectPool.closeall()

    # 执行增删改
    def execute(self, sql, value=None):
        conn, cursor = self.getConnect()
        try:
            res = cursor.execute(sql, value)
            conn.commit(conn, cursor)
            self.closeConnect(conn, cursor)
            return res
        except Exception as e:
            conn.rollock()
            raise e

    def select(self, sql):
        # logger.info('auto_draw_area_disparity_v1_202307 step1')
        try:
            conn, cursor = self.getConnect()
            #logger.info('connectPool getconn conn:%s cursor:%s poll:%s' % (conn.closed,cursor.closed,conn.poll()))
            cursor.execute(sql)
            # logger.info('auto_draw_area_disparity_v1_202307 step1')
            data = cursor.fetchall()
            # logger.info('auto_draw_area_disparity_v1_202307 step2')
            col_description = cursor.description
            # logger.info('auto_draw_area_disparity_v1_202307 step3')
            column_count = len(col_description)
            col_names = []
            result = {}
            for i in range(column_count):
                desc = col_description[i]
                col_names.append(desc[0])
            result['head'] = col_names
            if len(data) > 0:
                result['data'] = data
            else:
                result['data'] = [tuple([np.nan] * len(col_names))]
        except Exception as e:
            logger.error('holo connect error:%s' % MSG_FORMAT(e=e))
        #     conn.rollback()
        #     cursor.execute(sql)
        # conn.commit()
        # logger.info('auto_draw_area_disparity_v1_202307 step2')
        # logger.info('auto_draw_area_disparity_v1_202307 step2')
        self.closeConnect(conn, cursor)
        rs_df = pd.DataFrame(list(result.get('data')), columns=result.get('head'), dtype='str')
        return rs_df

if __name__ == "__main__":
    import sys

    if not (host and user and pwd):
        raise SystemExit(
            "credentials missing: set CREDIT_AGENT_PG_HOST / _DATABASE / _USER / _PASSWORD "
            "(see .env.example)"
        )
    client = Db_Holo(host=host, db=db, user=user, pwd=pwd, port=port)
    sql = sys.argv[1] if len(sys.argv) > 1 else "select 1"
    print(client.select(sql).head())