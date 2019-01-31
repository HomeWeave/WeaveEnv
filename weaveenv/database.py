import errno
import os

import appdirs
from peewee import SqliteDatabase, Proxy, Model, CharField, BooleanField
from peewee import DoesNotExist


proxy = Proxy()


def get_db_path():
    weave_base = appdirs.user_data_dir("homeweave")
    try:
        os.makedirs(weave_base)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    return os.path.join(weave_base, "weaveenv.db")


class BaseModel(Model):
    class Meta(object):
        database = proxy


class PluginData(BaseModel):
    app_id = CharField(unique=True)
    app_secret_token = CharField()
    enabled = BooleanField(default=False)


class PluginsDatabase(object):
    def __init__(self, path):
        self.path = path
        self.conn = SqliteDatabase(self.path)

    def start(self):
        proxy.initialize(self.conn)
        self.conn.create_tables([PluginData])

    def query(self, key):
        try:
            return PluginData.get(PluginData.app_id == key)
        except DoesNotExist:
            raise ValueError(key)

    def insert(self, **kwargs):
        query = PluginData.insert(**kwargs)
        query.on_conflict_replace().execute()
