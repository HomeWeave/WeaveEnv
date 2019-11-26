import json
import os
import shutil
import subprocess
import textwrap
from glob import glob
from pathlib import Path
from unittest.mock import patch

import pytest

from weavelib.exceptions import PluginLoadError
from weavelib.services import BasePlugin
from weavelib.messaging import WeaveConnection

from weaveenv.database import PluginData, WeaveEnvInstanceData, PluginsDatabase
from weaveenv.plugins import load_plugin_json, VirtualEnvManager, PluginManager
from weaveenv.plugins import url_to_plugin_id

from test_utils import MessagingService, DummyEnvService


class TestPluginLoadJson(object):
    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.tmpdir = tmpdir.strpath
        self.plugin_dir = os.path.join(self.tmpdir, "plugin")
        os.makedirs(self.plugin_dir)

    def write_content(self, path, contents):
        with open(os.path.join(self.plugin_dir, path), "w") as out:
            out.write(contents)

    def test_no_plugin_json(self):
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Error opening plugin.json."

    def test_invalid_json(self):
        self.write_content("plugin.json", "hello word.")

        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Error parsing plugin.json."

    def test_no_service_field(self):
        self.write_content("plugin.json", '{"hello": "world"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Required field not found in plugin.json."

    def test_bad_service_specification(self):
        self.write_content("plugin.json", '{"service": "hello world"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Bad 'service' specification in plugin.json."

    def test_failing_import(self):
        self.write_content("plugin.json", '{"service": "a.b.c"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Failed to import dependencies."

    def test_failing_import2(self):
        self.write_content("plugin.json", '{"service": "weaveenv.plugins.tmp"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra ==  "Bad service specification in plugin.json"

    def test_bad_dependency(self):
        spec = {
            "service": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10,
            "deps": [1, 2, 4],
            "required_rpc_classes": ["http", "bad"],
        }

        self.write_content("plugin.json", json.dumps(spec))
        with pytest.raises(PluginLoadError, match=".*dependencies.*"):
            load_plugin_json(self.plugin_dir, load_service=False)

    def test_bad_exported_rpc(self):
        spec = {
            "service": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10,
            "deps": [1, 2, 4],
            "exported_rpc_classes": {
                "http": "HTTP",
                "bad": ".."
            }
        }

        self.write_content("plugin.json", json.dumps(spec))
        with pytest.raises(PluginLoadError, match=".*rpc_class.*"):
            load_plugin_json(self.plugin_dir, load_service=False)

    def test_good_plugin_load(self):
        spec = {
            "service": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10,
            "deps": [1, 2, 4]
        }
        main_py = """
        from weavelib.services import BasePlugin

        class TestPlugin(BasePlugin):
            pass
        """
        package_dir = os.path.join(self.plugin_dir, "plugin")
        main_py_path = os.path.join(self.plugin_dir, "plugin", "main.py")

        os.makedirs(self.plugin_dir + "/plugin")
        self.write_content("plugin.json", json.dumps(spec))
        self.write_content(main_py_path, textwrap.dedent(main_py))

        expected = {
            "deps": [1, 2, 4],
            "package_path": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10,
            "service_name": "TestPlugin",
            "exported_rpc_classes": {},
            "required_rpc_classes": [],
        }

        plugin_info = load_plugin_json(self.plugin_dir)
        plugin_class = plugin_info.pop("service_cls")

        assert expected == plugin_info
        assert plugin_class.__name__ == "TestPlugin"


class TestVirtualEnvManager(object):
    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.tmpdir = tmpdir.strpath
        self.venv_dir = os.path.join(self.tmpdir, "venv")
        self.tmp_dir = os.path.join(self.tmpdir, "scratch")
        os.makedirs(self.tmp_dir)

    def teardown(self):
        shutil.rmtree(self.tmpdir)

    def write_content(self, path, contents):
        with open(os.path.join(self.tmp_dir, path), "w") as out:
            out.write(contents)

    def test_create_virtual_env(self):
        venv = VirtualEnvManager(Path(self.venv_dir))

        assert venv.install()
        assert venv.install()

    def test_create_virtual_env_with_bad_requirements(self):
        self.write_content("requirements.txt", "skdjdlss_slkdjs")

        venv = VirtualEnvManager(Path(self.venv_dir))

        requirements_file = Path(self.tmp_dir) / "requirements.txt"
        assert not venv.install(requirements_file)

    def test_create_virtual_env_with_requirements(self):
        self.write_content("requirements.txt", "bottle")

        venv = VirtualEnvManager(Path(self.venv_dir))

        requirements_file = Path(self.tmp_dir) / "requirements.txt"
        assert venv.install(requirements_file)

        python = os.path.join(self.venv_dir, "bin/python")
        command = [python, "-c", "import bottle"]
        subprocess.check_call(command)

        assert venv.is_installed()

        venv.clean()

        assert not venv.is_installed()


@patch('git.Repo', clone_from=shutil.copytree)
@patch.object(BasePlugin, 'service_start', return_value=None)
@patch.object(BasePlugin, 'service_stop', return_value=None)
@patch.object(BasePlugin, 'wait_for_start', return_value=True)
class TestPluginLifecycle(object):
    def get_test_id(self, plugin_dir):
        return url_to_plugin_id(self.get_test_plugin_path(plugin_dir))

    def get_test_plugin_path(self, plugin_dir):
        testdata = os.path.join(os.path.dirname(__file__), "testdata")
        return os.path.join(testdata, plugin_dir)

    def get_test_plugin(self, dir_name):
        path = self.get_test_plugin_path(dir_name)
        return path, dir_name, "description"

    def list_plugins(self):
        pattern = os.path.join(os.path.dirname(__file__), "testdata/*/")
        plugin_dirs = [os.path.basename(x.rstrip('/')) for x in glob(pattern)]
        return [self.get_test_plugin(x) for x in plugin_dirs]

    @classmethod
    def setup_class(cls):
        cls.messaging_service = MessagingService()
        cls.messaging_service.service_start()
        cls.messaging_service.wait_for_start(15)

        cls.conn = WeaveConnection.local()
        cls.conn.connect()

        cls.fake_service = DummyEnvService(cls.messaging_service.test_token,
                                          cls.conn)

    @classmethod
    def teardown_class(cls):
        cls.conn.close()
        cls.messaging_service.service_stop()

    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.base_dir = Path(tmpdir.strpath)
        self.db = PluginsDatabase(":memory:")
        self.db.start()
        self.instance = WeaveEnvInstanceData(machine_id="x", app_token="y")
        self.instance.save(force_insert=True)

    def teardown(self):
        shutil.rmtree(self.base_dir)

    def test_plugin_listing(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()

        expected = [
            {
                "name": "plugin1",
                "description": "description",
                "id": self.get_test_id('plugin1'),
                "enabled": False,
                "installed": False,
                "active": False,
                "remote_url": self.get_test_plugin_path('plugin1'),
            }
        ]

        assert pm.list() == expected

    def test_plugin_install(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()
        res = pm.install(self.get_test_plugin_path('plugin1'))

        expected = {
            "name": "plugin1",
            "description": "description",
            "id": self.get_test_id('plugin1'),
            "enabled": False,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

        assert res == expected

    def test_plugin_double_install(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()
        res1 = pm.install(self.get_test_plugin_path('plugin1'))

        expected = {
            "name": "plugin1",
            "description": "description",
            "id": self.get_test_id('plugin1'),
            "enabled": False,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

        res2 = pm.install(self.get_test_plugin_path('plugin1'))
        assert res1 == res2 == expected

    def test_plugin_uninstall(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()
        res1 = pm.install(self.get_test_plugin_path('plugin1'))
        res2 = pm.uninstall(self.get_test_plugin_path('plugin1'))
        expected = {
            "name": "plugin1",
            "description": "description",
            "id": self.get_test_id('plugin1'),
            "enabled": False,
            "installed": False,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

        assert res2 == expected

    def test_plugin_bad_uninstall(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()
        res = pm.uninstall(self.get_test_plugin_path('plugin1'))
        assert not res["installed"]

    def test_uninstall_enabled_plugin(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()
        plugin_url = self.get_test_plugin_path('plugin1')
        pm.install(plugin_url)
        pm.enable(plugin_url)

        with pytest.raises(PluginLoadError, match="Must disable the plugin .*"):
            pm.uninstall(self.get_test_plugin_path('plugin1'))

        pm.stop()

    def test_load_plugin_not_enabled(self, *args):
        plugin_url = self.get_test_plugin_path('plugin1')
        db_plugin = PluginData(name="plugin1", description="x",
                               app_url=plugin_url, machine=self.instance)
        db_plugin.save(force_insert=True)

        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()

        assert pm.plugin_state(plugin_url) == {
            "name": "plugin1",
            "description": "description",
            "id": self.get_test_id('plugin1'),
            "enabled": False,
            "installed": False,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

    def test_load_plugin_enabled(self, *args):
        plugin_url = self.get_test_plugin_path('plugin1')
        plugin_id = self.get_test_id('plugin1')
        db_plugin = PluginData(name="plugin1", description="x", enabled=True,
                               app_url=plugin_url, machine=self.instance)
        install_dir = self.base_dir / "plugins" / plugin_id
        venv_dir = self.base_dir / "venv" / plugin_id

        db_plugin.save(force_insert=True)
        shutil.copytree(plugin_url, str(install_dir))

        VirtualEnvManager(venv_dir).install(install_dir / "requirements.txt")

        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()

        expected = {
            "name": "plugin1",
            "description": "description",
            "id": plugin_id,
            "enabled": True,
            "installed": True,
            "active": True,
            "remote_url": plugin_url,
        }
        assert pm.plugin_state(plugin_url) == expected

        pm.stop()

    def test_activate(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        pm.install(plugin_url)
        pm.enable(plugin_url)
        result = pm.activate(plugin_url)

        expected = {
            "name": "plugin1",
            "description": "description",
            "id": self.get_test_id('plugin1'),
            "enabled": True,
            "installed": True,
            "active": True,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }
        assert result == expected

        result = pm.deactivate(plugin_url)
        expected = {
            "name": "plugin1",
            "description": "description",
            "id": self.get_test_id('plugin1'),
            "enabled": True,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }
        assert result == expected

        pm.stop()

    def test_activate_disabled_plugin(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm.install(plugin_url)

        with pytest.raises(PluginLoadError, match=".*not enabled.*"):
            pm.activate(plugin_url)

    def test_activate_remote_plugin(self, *args):
        pm = PluginManager(self.base_dir, self.instance, self.fake_service,
                           lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        with pytest.raises(PluginLoadError, match=".*not installed.*"):
            pm.activate(plugin_url)
