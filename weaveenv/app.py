import eventlet
eventlet.monkey_patch()  # NOLINT

import errno
import json
import logging.config
import os
import signal
import subprocess
import sys
from uuid import uuid4

import appdirs
from peewee import DoesNotExist

from weavelib.exceptions import ObjectNotFound
from weavelib.messaging import WeaveConnection
from weavelib.services.service_base import MessagingEnabled

from weaveenv.database import PluginsDatabase, WeaveEnvInstanceData, PluginData
from weaveenv.http import WeaveHTTPServer
from weaveenv.instances import get_plugin_by_url, LocalWeaveInstance
from weaveenv.plugins import PluginManager, VirtualEnvManager, GitPlugin
from weaveenv.plugins import url_to_plugin_id, load_plugin_json
from weaveenv.logging import LOGGING


logging.config.dictConfig(LOGGING)

MESSAGING_PLUGIN_URL = "https://github.com/HomeWeave/WeaveServer.git"


def get_config_path():
    if os.environ.get("WEAVE_DIR"):
        return os.environ["WEAVE_DIR"]

    weave_base = appdirs.user_data_dir("homeweave")
    try:
        os.makedirs(weave_base)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    return weave_base


def get_machine_id():
    if sys.platform == 'darwin':
        command = ['system_profiler', 'SPHardwareDataType']
        process = subprocess.Popen(command, stdout=subprocess.PIPE)
        out, err = process.communicate()
        lines = [x.strip().decode('UTF-8') for x in out.splitlines()]
        hardware_line = next(x for x in lines if 'Hardware UUID' in x)
        return hardware_line.split()[1].strip()
    with open("/sys/class/dmi/id/modalias") as inp:
        return inp.read()


def handle_main():
    machine_id = get_machine_id()
    base_path = get_config_path()
    plugins_db = PluginsDatabase(os.path.join(base_path, "db"))
    plugin_manager = PluginManager(base_path)

    plugins_db.start()

    try:
        instance_data = WeaveEnvInstanceData.get(WeaveEnvInstanceData.machine_id
                                                 == machine_id)
    except DoesNotExist:
        sys.exit("Please re-install messaging plugin.")

    weave = LocalWeaveInstance(instance_data, plugin_manager)
    weave.start()

    signal.signal(signal.SIGTERM, lambda x, y: weave.stop())
    signal.signal(signal.SIGINT, lambda x, y: weave.stop())

    weave.wait()


def handle_messaging_plugin_install():
    machine_id = get_machine_id()
    base_path = get_config_path()
    plugins_db = PluginsDatabase(os.path.join(base_path, "db"))
    plugin_manager = PluginManager(base_path)

    plugins_db.start()

    git_plugin = GitPlugin(MESSAGING_PLUGIN_URL, "WeaveServer", "Messaging")
    plugin_manager.install(git_plugin)

    token = "app-token-" + str(uuid4())
    instance_data = WeaveEnvInstanceData(machine_id=machine_id, app_token=token)
    plugin_data = PluginData(app_id=url_to_plugin_id(MESSAGING_PLUGIN_URL),
                             name="WeaveServer", description="Messaging",
                             enabled=True, machine=instance_data)
    plugin_data.save(force_insert=True)
    instance_data.save(force_insert=True)


def handle_messaging_token():
    plugins_db = PluginsDatabase(os.path.join(get_config_path(), "db"))
    plugins_db.start()

    messaging_server_url = MESSAGING_PLUGIN_URL
    if sys.argv[1] == 'set':
        plugins_db.insert(app_id=url_to_plugin_id(messaging_server_url),
                          app_secret_token=sys.argv[2], is_remote=True)
    elif sys.argv[1] == 'get':
        plugin_data = plugins_db.query(url_to_plugin_id(messaging_server_url))
        print(plugin_data.app_secret_token)
    else:
        print("Supported operations: 'get' and 'set'")


def handle_weave_launch():
    params = json.loads(sys.stdin.readline().strip())

    plugin_dir = params["plugin_dir"]
    os.chdir(plugin_dir)
    sys.path.append(plugin_dir)

    if params.get("venv_dir"):
        venv = VirtualEnvManager(params["venv_dir"])
        venv.activate()

    ignore_hierarchy = bool(params.get("ignore_hierarchy"))
    plugin_info = load_plugin_json(plugin_dir,
                                   ignore_hierarchy=ignore_hierarchy)

    if issubclass(plugin_info["service_cls"], MessagingEnabled):
        conn = WeaveConnection.discover()
        conn.connect()
        params["conn"] = conn

    app = plugin_info["service_cls"](**params)

    signal.signal(signal.SIGTERM, lambda x, y: app.on_service_stop())
    signal.signal(signal.SIGINT, lambda x, y: app.on_service_stop())

    app.before_service_start()
    app.on_service_start()


def handle_discover():
    conn = WeaveConnection.discover()
    print(conn.default_host, conn.default_port)
