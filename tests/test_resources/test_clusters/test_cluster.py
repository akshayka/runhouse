import subprocess
import time

import pandas as pd
import pytest
import requests

import runhouse as rh

from runhouse.constants import (
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_SERVER_PORT,
    LOCALHOST,
    SERVER_LOGFILE_PATH,
)

import tests.test_resources.test_resource
from tests.conftest import init_args
from tests.utils import (
    friend_account,
    friend_account_in_org,
    get_random_str,
    remove_config_keys,
)

""" TODO:
1) In subclasses, test factory methods create same type as parent
2) In subclasses, use monkeypatching to make sure `up()` is called for various methods if the server is not up
3) Test AWS, GCP, and Azure static clusters separately
"""


def load_shared_resource_config(resource_class_name, address):
    resource_class = getattr(rh, resource_class_name)
    loaded_resource = resource_class.from_name(address, dryrun=True)
    return loaded_resource.config()


def save_resource_and_return_config():
    df = pd.DataFrame(
        {"id": [1, 2, 3, 4, 5, 6], "grade": ["a", "b", "b", "a", "a", "e"]}
    )
    table = rh.table(df, name="test_table")
    return table.config()


def test_table_to_rh_here():
    df = pd.DataFrame(
        {"id": [1, 2, 3, 4, 5, 6], "grade": ["a", "b", "b", "a", "a", "e"]}
    )
    rh.table(df, name="test_table").to(rh.here)
    assert rh.here.get("test_table") is not None


def summer(a: int, b: int):
    return a + b


def sub(a: int, b: int):
    return a - b


def cluster_keys(cluster):
    return cluster.keys()


def cluster_config():
    return rh.here.config()


def assume_caller_and_get_token():
    token_default = rh.configs.token
    with rh.as_caller():
        token_as_caller = rh.configs.token
    return token_default, token_as_caller


class TestCluster(tests.test_resources.test_resource.TestResource):
    MAP_FIXTURES = {"resource": "cluster"}

    UNIT = {"cluster": ["named_cluster"]}
    LOCAL = {
        "cluster": [
            "docker_cluster_pk_ssh_no_auth",  # Represents private dev use case
            "docker_cluster_pk_ssh_den_auth",  # Helps isolate Auth issues
            "docker_cluster_pk_tls_den_auth",  # Represents public app use case
            "docker_cluster_pk_http_exposed",  # Represents within VPC use case
            "docker_cluster_pwd_ssh_no_auth",
        ],
    }
    MINIMAL = {"cluster": ["static_cpu_cluster"]}
    RELEASE = {
        "cluster": [
            "static_cpu_cluster",
            "password_cluster",
        ]
    }
    MAXIMAL = {
        "cluster": [
            "docker_cluster_pk_ssh_no_auth",
            "docker_cluster_pk_ssh_den_auth",
            "docker_cluster_pwd_ssh_no_auth",
            "static_cpu_cluster",
            "password_cluster",
            "multinode_cpu_cluster",
        ]
    }

    GPU_CLUSTER_NAMES = ["rh-v100", "rh-k80", "rh-a10x", "rh-gpu-multinode"]

    @pytest.mark.level("unit")
    def test_cluster_factory_and_properties(self, cluster):
        assert isinstance(cluster, rh.Cluster)
        args = init_args[id(cluster)]
        if "ips" in args:
            # Check that it's a Cluster and not a subclass
            assert cluster.__class__.name == "Cluster"
            assert cluster.ips == args["ips"]
            assert cluster.address == args["ips"][0]

        if "ssh_creds" in args:
            cluster_creds = cluster.creds_values
            if "ssh_private_key" in cluster_creds:
                # this means that the secret was created by accessing an ssh-key file
                cluster_creds.pop("private_key", None)
                cluster_creds.pop("public_key", None)
            assert cluster_creds == args["ssh_creds"]

        if "server_host" in args:
            assert cluster.server_host == args["server_host"]
        else:
            assert cluster.server_host is None

        if "ssl_keyfile" in args:
            assert cluster.cert_config.key_path == args["ssl_keyfile"]

        if "ssl_certfile" in args:
            assert cluster.cert_config.cert_path == args["ssl_certfile"]

    @pytest.mark.level("local")
    def test_docker_cluster_fixture_is_logged_out(self, docker_cluster_pk_ssh_no_auth):
        save_resource_and_return_config_cluster = rh.function(
            save_resource_and_return_config,
            name="save_resource_and_return_config_cluster",
        ).to(
            system=docker_cluster_pk_ssh_no_auth,
        )
        saved_config_on_cluster = save_resource_and_return_config_cluster()
        # This cluster was created without any logged in Runhouse config. Make sure that the simple resource
        # created on the cluster starts with "~", which is the prefix that local Runhouse configs are saved with.
        assert ("/" not in saved_config_on_cluster["name"]) or (
            saved_config_on_cluster["name"].startswith("~")
        )

    @pytest.mark.level("local")
    def test_cluster_recreate(self, cluster):
        # Create underlying ssh connection if not already
        cluster.run(["echo hello"])
        num_open_tunnels = len(rh.globals.sky_ssh_runner_cache)

        # Create a new cluster object for the same remote cluster
        cluster.save()
        new_cluster = rh.cluster(cluster.rns_address)
        new_cluster.run(["echo hello"])
        # Check that the same underlying ssh connection was used
        assert len(rh.globals.sky_ssh_runner_cache) == num_open_tunnels

    @pytest.mark.level("local")
    def test_cluster_endpoint(self, cluster):
        if not cluster.address:
            assert cluster.endpoint() is None
            return

        endpoint = cluster.endpoint()
        if cluster.server_connection_type in ["ssh", "aws_ssm"]:
            assert cluster.endpoint(external=True) is None
            assert endpoint == f"http://{LOCALHOST}:{cluster.client_port}"
        else:
            url_base = "https" if cluster.server_connection_type == "tls" else "http"
            if cluster.client_port not in [DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT]:
                assert (
                    endpoint
                    == f"{url_base}://{cluster.server_address}:{cluster.client_port}"
                )
            else:
                assert endpoint == f"{url_base}://{cluster.server_address}"

        # Try to curl docs
        verify = cluster.client.verify
        r = requests.get(
            f"{endpoint}/status",
            verify=verify,
            headers=rh.globals.rns_client.request_headers(),
        )
        assert r.status_code == 200
        assert r.json().get("cluster_config")["resource_type"] == "cluster"

    @pytest.mark.level("local")
    def test_load_cluster_status(self, cluster):
        endpoint = cluster.endpoint()
        verify = cluster.client.verify
        r = requests.get(
            f"{endpoint}/status",
            verify=verify,
            headers=rh.globals.rns_client.request_headers(),
        )

        assert r.status_code == 200
        status_data = r.json()
        assert status_data["cluster_config"]["resource_type"] == "cluster"
        assert status_data["env_servlet_processes"]
        assert status_data["system_cpu_usage"]
        assert status_data["system_memory_usage"]
        assert status_data["system_disk_usage"]
        assert not status_data.get("system_gpu_data")

    @pytest.mark.level("local")
    def test_cluster_objects(self, cluster):
        k1 = get_random_str()
        k2 = get_random_str()
        cluster.put(k1, "v1")
        cluster.put(k2, "v2")
        assert k1 in cluster.keys()
        assert k2 in cluster.keys()
        assert cluster.get(k1) == "v1"
        assert cluster.get(k2) == "v2"

        # Make new env
        rh.env(reqs=["numpy"], name="numpy_env").to(cluster)
        assert "numpy_env" in cluster.keys()

        k3 = get_random_str()
        cluster.put(k3, "v3", env="numpy_env")
        assert k3 in cluster.keys()
        assert cluster.get(k3) == "v3"

    @pytest.mark.level("local")
    def test_cluster_delete_env(self, cluster):
        env1 = rh.env(reqs=[], working_dir="./", name="env1").to(cluster)
        env2 = rh.env(reqs=[], working_dir="./", name="env2").to(cluster)
        env3 = rh.env(reqs=[], working_dir="./", name="env3")

        cluster.put("k1", "v1", env=env1.name)
        cluster.put("k2", "v2", env=env2.name)
        cluster.put_resource(env3, env=env1.name)

        # test delete env2
        assert cluster.get(env2.name)
        assert cluster.get("k2")

        cluster.delete(env2.name)
        assert not cluster.get(env2.name)
        assert not cluster.get("k2")

        # test delete env3, which doesn't affect env1
        assert cluster.get(env3.name)

        cluster.delete(env3.name)
        assert not cluster.get(env3.name)
        assert cluster.get(env1.name)
        assert cluster.get("k1")

    @pytest.mark.level("local")
    @pytest.mark.skip(reason="TODO")
    def test_rh_here_objects(self, cluster):
        save_test_table_remote = rh.function(test_table_to_rh_here, system=cluster)
        save_test_table_remote()
        assert "test_table" in cluster.keys()
        assert isinstance(cluster.get("test_table"), rh.Table)

    @pytest.mark.level("local")
    def test_rh_status_pythonic(self, cluster):
        rh.env(reqs=["pytest"], name="worker_env").to(cluster)
        cluster.put(key="status_key1", obj="status_value1", env="worker_env")

        cluster_data = cluster.status()

        expected_cluster_status_data_keys = [
            "env_servlet_processes",
            "env_resource_mapping",
            "server_pid",
            "runhouse_version",
            "cluster_config",
        ]

        actual_cluster_status_data_keys = list(cluster_data.keys())

        for key in expected_cluster_status_data_keys:
            assert key in actual_cluster_status_data_keys

        res = cluster_data.get("cluster_config")

        # test cluster config info
        assert res.get("creds") is None
        assert res.get("server_port") == (cluster.server_port or DEFAULT_SERVER_PORT)
        assert res.get("server_connection_type") == cluster.server_connection_type
        assert res.get("den_auth") == cluster.den_auth
        assert res.get("resource_type") == cluster.RESOURCE_TYPE
        assert res.get("ips") == cluster.ips

        assert "worker_env" in cluster_data.get("env_resource_mapping")
        assert {
            "name": "status_key1",
            "resource_type": "str",
            "active_function_calls": [],
        } in cluster_data.get("env_resource_mapping")["worker_env"]

        # test memory usage info
        expected_env_servlet_keys = [
            "env_gpu_usage",
            "env_memory_usage",
            "node_ip",
            "node_name",
            "pid",
        ]
        envs_names = list(cluster_data.get("env_resource_mapping").keys())
        envs_names.sort()
        assert "env_servlet_processes" in cluster_data.keys()
        env_servlets_info = cluster_data.get("env_servlet_processes")
        env_actors_keys = list(env_servlets_info.keys())
        env_actors_keys.sort()
        assert envs_names == env_actors_keys
        for env_name in envs_names:
            env_servlet_info = env_servlets_info.get(env_name)
            env_servlet_info_keys = list(env_servlet_info.keys())
            env_servlet_info_keys.sort()
            assert env_servlet_info_keys == expected_env_servlet_keys

            if cluster.name in self.GPU_CLUSTER_NAMES and env_name == "sd_env":
                assert env_servlet_info.get("env_gpu_usage")

    @pytest.mark.level("maximal")
    def test_rh_status_pythonic_gpu(self, cluster):
        if cluster.name in self.GPU_CLUSTER_NAMES:
            from tests.test_tutorials import sd_generate

            env_sd = rh.env(
                reqs=["pytest", "diffusers", "torch", "transformers"],
                name="sd_env",
                compute={"GPU": 1, "CPU": 4},
            ).to(system=cluster, force_install=True)

            assert env_sd

            generate_gpu = rh.function(fn=sd_generate).to(system=cluster, env=env_sd)

            images = generate_gpu(
                prompt="A hot dog made of matcha powder.", num_images=4, steps=50
            )

            assert images

            self.test_rh_status_pythonic(cluster)

        else:
            pytest.skip(f"{cluster.name} is not a GPU cluster, skipping")

    @pytest.mark.level("local")
    def test_rh_status_cli_in_cluster(self, cluster):
        default_env_name = cluster.default_env.name

        cluster.put(key="status_key2", obj="status_value2")
        status_output_string = cluster.run(
            ["runhouse status"], _ssh_mode="non_interactive"
        )[0][1]
        # The string that's returned is utf-8 with the literal escape characters mixed in.
        # We need to convert the escape characters to their actual values to compare the strings.
        status_output_string = status_output_string.encode("utf-8").decode(
            "unicode_escape"
        )
        status_output_string = status_output_string.replace("\n", "")
        assert "Runhouse Daemon is running" in status_output_string
        assert f"Runhouse v{rh.__version__}" in status_output_string
        assert f"server port: {cluster.server_port}" in status_output_string
        assert (
            f"server connection type: {cluster.server_connection_type}"
            in status_output_string
        )
        assert f"den auth: {str(cluster.den_auth)}" in status_output_string
        assert (
            f"resource subtype: {cluster.config().get('resource_subtype')}"
            in status_output_string
        )
        assert f"ips: {str(cluster.ips)}" in status_output_string
        assert "Serving " in status_output_string
        assert (
            f"{default_env_name} (runhouse.Env)" in status_output_string
            or f"{default_env_name} (runhouse.CondaEnv)" in status_output_string
        )
        assert "status_key2 (str)" in status_output_string
        assert "creds" not in status_output_string

        # checking the memory info is printed correctly
        assert "CPU: " in status_output_string
        assert status_output_string.count("CPU: ") >= 1
        assert "pid: " in status_output_string
        assert status_output_string.count("pid: ") >= 1
        assert "node: " in status_output_string
        assert status_output_string.count("node: ") >= 1

        # if it is a GPU cluster, check GPU print as well
        if cluster.name in self.GPU_CLUSTER_NAMES:
            assert "GPU: " in status_output_string
            assert status_output_string.count("GPU: ") >= 1

    @pytest.mark.level("maximal")
    def test_rh_status_cli_in_gpu_cluster(self, cluster):
        if cluster.name in self.GPU_CLUSTER_NAMES:
            from tests.test_tutorials import sd_generate

            env_sd = rh.env(
                reqs=["pytest", "diffusers", "torch", "transformers"],
                name="sd_env",
                compute={"GPU": 1},
            ).to(system=cluster, force_install=True)

            assert env_sd
            generate_gpu = rh.function(fn=sd_generate).to(system=cluster, env=env_sd)
            images = generate_gpu(
                prompt="A hot dog made of matcha powder.", num_images=4, steps=50
            )
            assert images

            self.test_rh_status_cli_in_cluster(cluster)

        else:
            pytest.skip(f"{cluster.name} is not a GPU cluster, skipping")

    @pytest.mark.skip("Restarting the server mid-test causes some errors, need to fix")
    @pytest.mark.level("local")
    # TODO: once fixed, extend this tests for gpu clusters as well.
    def test_rh_status_cli_not_in_cluster(self, cluster):
        default_env_name = cluster.default_env.name

        cluster.put(key="status_key3", obj="status_value3")
        res = str(
            subprocess.check_output(["runhouse", "status", f"{cluster.name}"]), "utf-8"
        )
        assert "😈 Runhouse Daemon is running 🏃" in res
        assert f"server port: {cluster.server_port}" in res
        assert f"server connection_type: {cluster.server_connection_type}" in res
        assert f"den auth: {str(cluster.den_auth)}" in res
        assert f"resource subtype: {cluster.RESOURCE_TYPE.capitalize()}" in res
        assert f"ips: {str(cluster.ips)}" in res
        assert "Serving 🍦 :" in res
        assert f"{default_env_name} (runhouse.Env)" in res
        assert "status_key3 (str)" in res
        assert "ssh certs" not in res

    @pytest.mark.skip("Restarting the server mid-test causes some errors, need to fix")
    @pytest.mark.level("local")
    # TODO: once fixed, extend this tests for gpu clusters as well.
    def test_rh_status_stopped(self, cluster):
        try:
            cluster_name = cluster.name
            cluster.run(["runhouse stop"])
            res = subprocess.check_output(["runhouse", "status", cluster_name]).decode(
                "utf-8"
            )
            assert "Runhouse Daemon is not running" in res
            res = subprocess.check_output(
                ["runhouse", "status", f"{cluster_name}_dont_exist"]
            ).decode("utf-8")
            error_txt = (
                f"Cluster {cluster_name}_dont_exist is not found in Den. Please save it, in order to get "
                f"its status"
            )
            assert error_txt in res
        finally:
            cluster.run(["runhouse restart"])

    @pytest.mark.level("local")
    def test_condensed_config_for_cluster(self, cluster):
        remote_cluster_config = rh.function(cluster_config).to(cluster)
        on_cluster_config = remote_cluster_config()
        local_cluster_config = cluster.config()

        keys_to_skip = [
            "creds",
            "client_port",
            "server_host",
            "api_server_url",
            "ssl_keyfile",
            "ssl_certfile",
        ]
        on_cluster_config = remove_config_keys(on_cluster_config, keys_to_skip)
        local_cluster_config = remove_config_keys(local_cluster_config, keys_to_skip)

        if local_cluster_config.get("stable_internal_external_ips", False):
            cluster_ips = local_cluster_config.pop(
                "stable_internal_external_ips", None
            )[0]
            on_cluster_ips = on_cluster_config.pop(
                "stable_internal_external_ips", None
            )[0]
            assert tuple(cluster_ips) == tuple(on_cluster_ips)

        assert on_cluster_config == local_cluster_config

    @pytest.mark.level("local")
    def test_sharing(self, cluster, friend_account_logged_in_docker_cluster_pk_ssh):
        # Skip this test for ondemand clusters, because making
        # it compatible with ondemand_cluster requires changes
        # that break CI.
        # TODO: Remove this by doing some CI-specific logic.
        if cluster.__class__.__name__ == "OnDemandCluster":
            return

        if cluster.rns_address.startswith("~"):
            # For `local_named_resource` resolve the rns address so it can be shared and loaded
            from runhouse.globals import rns_client

            cluster.rns_address = rns_client.local_to_remote_address(
                cluster.rns_address
            )

        cluster.share(
            users=["info@run.house"],
            access_level="read",
            notify_users=False,
        )

        # First try loading in same process/filesystem because it's more debuggable, but not as thorough
        resource_class_name = cluster.config().get("resource_type").capitalize()
        config = cluster.config()

        with friend_account():
            curr_config = load_shared_resource_config(
                resource_class_name, cluster.rns_address
            )
            new_creds = curr_config.get("creds", None)
            assert f'{config["name"]}-ssh-secret' in new_creds
            assert curr_config == config

        # TODO: If we are testing with an ondemand_cluster we to
        # sync sky key so loading ondemand_cluster from config works
        # Also need aws secret to load availability zones
        # secrets=["sky", "aws"],
        load_shared_resource_config_cluster = rh.function(
            load_shared_resource_config
        ).to(friend_account_logged_in_docker_cluster_pk_ssh)
        new_config = load_shared_resource_config_cluster(
            resource_class_name, cluster.rns_address
        )
        new_creds = curr_config.get("creds", None)
        assert f'{config["name"]}-ssh-secret' in new_creds
        assert new_config == config

    @pytest.mark.level("local")
    def test_access_to_shared_cluster(self, cluster):
        # TODO: Remove this by doing some CI-specific logic.
        if cluster.__class__.__name__ == "OnDemandCluster":
            return

        if cluster.rns_address.startswith("~"):
            # For `local_named_resource` resolve the rns address so it can be shared and loaded
            from runhouse.globals import rns_client

            cluster.rns_address = rns_client.local_to_remote_address(
                cluster.rns_address
            )

        cluster.share(
            users=["support@run.house"],
            access_level="write",
            notify_users=False,
        )

        cluster_name = cluster.rns_address
        cluster_creds = cluster.creds_values
        cluster_creds.pop("private_key", None)
        cluster_creds.pop("public_key", None)

        with friend_account_in_org():
            shared_cluster = rh.cluster(name=cluster_name)
            assert shared_cluster.rns_address == cluster_name
            assert shared_cluster.creds_values.keys() == cluster_creds.keys()
            echo_msg = "hello from shared cluster"
            run_res = shared_cluster.run([f"echo {echo_msg}"])
            assert echo_msg in run_res[0][1]
            # First element, return code
            assert shared_cluster.run(["echo hello"])[0][0] == 0

    @pytest.mark.level("local")
    def test_changing_name_and_saving_in_between(self, cluster):
        remote_summer = rh.function(summer).to(cluster)
        assert remote_summer(3, 4) == 7
        old_name = cluster.name

        cluster.save(name="new_testing_name")

        assert remote_summer(3, 4) == 7
        remote_sub = rh.function(sub).to(cluster)
        assert remote_sub(3, 4) == -1

        cluster_keys_remote = rh.function(cluster_keys).to(cluster)

        # If save did not update the name, this will attempt to create a connection
        # when the cluster is used remotely. However, if you update the name, `on_this_cluster` will
        # work correctly and then the remote function will just call the object store when it calls .keys()
        assert cluster.keys() == cluster_keys_remote(cluster)

        # Restore the state?
        cluster.save(name=old_name)

    @pytest.mark.level("local")
    def test_caller_token_propagated(self, cluster):
        remote_assume_caller_and_get_token = rh.function(
            assume_caller_and_get_token
        ).to(cluster)

        remote_assume_caller_and_get_token.share(
            users=["info@run.house"], notify_users=False
        )

        with friend_account():
            unassumed_token, assumed_token = remote_assume_caller_and_get_token()
            # "Local token" is the token the cluster accesses in rh.configs.token; this is what will be used
            # in subsequent rns_client calls
            assert assumed_token == rh.globals.rns_client.cluster_token(
                rh.configs.token, cluster.rns_address
            )
            assert unassumed_token != rh.configs.token

        # Docker clusters are logged out, ondemand clusters are logged in
        output = cluster.run("sed -n 's/.*token: *//p' ~/.rh/config.yaml")
        # No config file
        if output[0][0] == 2:
            assert unassumed_token is None
        elif output[0][0] == 0:
            assert unassumed_token == output[0][1].strip()

    @pytest.mark.level("local")
    def test_send_status_to_db(self, cluster):
        import json

        if not cluster.den_auth:
            pytest.skip(
                "This test checking pinging cluster status to den, this could be done only on clusters "
                "with den_auth that can be saved to den."
            )

        cluster.save()

        status = cluster.status()
        status_data = {
            "status": "running",
            "resource_type": status.get("cluster_config").get("resource_type"),
            "data": dict(status),
        }
        cluster_uri = rh.globals.rns_client.format_rns_address(cluster.rns_address)
        headers = rh.globals.rns_client.request_headers()
        api_server_url = rh.globals.rns_client.api_server_url
        post_status_data_resp = requests.post(
            f"{api_server_url}/resource/{cluster_uri}/cluster/status",
            data=json.dumps(status_data),
            headers=headers,
        )
        assert post_status_data_resp.status_code in [200, 422]
        get_status_data_resp = requests.get(
            f"{api_server_url}/resource/{cluster_uri}/cluster/status",
            headers=headers,
        )
        assert get_status_data_resp.status_code == 200
        get_status_data = get_status_data_resp.json()["data"][0]
        assert get_status_data["resource_type"] == status.get("cluster_config").get(
            "resource_type"
        )
        assert get_status_data["status"] == "running"
        assert get_status_data["data"] == dict(status)

        status_data["status"] = "terminated"
        post_status_data_resp = requests.post(
            f"{api_server_url}/resource/{cluster_uri}/cluster/status",
            data=json.dumps(status_data),
            headers=headers,
        )
        assert post_status_data_resp.status_code == 200
        get_status_data_resp = requests.get(
            f"{api_server_url}/resource/{cluster_uri}/cluster/status",
            headers=headers,
        )
        assert get_status_data_resp.json()["data"][0]["status"] == "terminated"

    @pytest.mark.level("minimal")
    def test_status_scheduler_basic_flow(self, cluster):
        if not cluster.den_auth:
            pytest.skip(
                "This test checking pinging cluster status to den, this could be done only on clusters "
                "with den_auth that can be saved to den."
            )
        if not cluster.config().get("resource_subtype") == "OnDemandCluster":
            pytest.skip(
                "This test checking pinging cluster status to den, this could be done only on OnDemand clusters."
            )

        cluster.save()
        # the scheduler start running in a delay of 1 min, so the cluster startup will finish properly.
        # Therefore, the test needs to sleep for a while.
        time.sleep(60)
        cluster_logs = cluster.run([f"cat {SERVER_LOGFILE_PATH}"])[0][1]
        assert (
            "Performing cluster status check: potentially sending to Den or updating autostop."
            in cluster_logs
        )

        cluster_uri = rh.globals.rns_client.format_rns_address(cluster.rns_address)
        headers = rh.globals.rns_client.request_headers()
        api_server_url = rh.globals.rns_client.api_server_url

        get_status_data_resp = requests.get(
            f"{api_server_url}/resource/{cluster_uri}/cluster/status",
            headers=headers,
        )

        assert get_status_data_resp.status_code == 200
        assert get_status_data_resp.json()["data"][0]["status"] == "running"
