import json
import logging
import re
import retrying
from toolz import get_in
from typing import Any, Dict, List, Match, Optional, Union

import sdk_cmd
import sdk_hosts
import sdk_install
import sdk_networks
import sdk_repository
import sdk_service
import sdk_upgrade
import sdk_utils

log = logging.getLogger(__name__)

PACKAGE_NAME = "elastic"
SERVICE_NAME = "elastic"

DEFAULT_ELASTICSEARCH_USER = "elastic"
DEFAULT_ELASTICSEARCH_PASSWORD = "changeme"
DEFAULT_KIBANA_USER = "kibana"
DEFAULT_KIBANA_PASSWORD = "changeme"

KIBANA_PACKAGE_NAME = "kibana"
KIBANA_SERVICE_NAME = "kibana"
KIBANA_DEFAULT_TIMEOUT = 5 * 60

# sum of default pod counts, with one task each:
# - master: 3
# - data: 2
# - ingest: 0
# - coordinator: 1
DEFAULT_NODES_COUNT = 6
# - exporter 1
DEFAULT_TASK_COUNT = DEFAULT_NODES_COUNT + 1
# TODO: add and use throughout a method to determine expected task count based on options.
#       the method should provide for use cases:
#         * total count, ie 6
#         * count for a specific type, ie 3
#         * count by type, ie [{'ingest':1},{'data':3},...]

DEFAULT_TIMEOUT = 30 * 60
DEFAULT_INDEX_NAME = "customer"
DEFAULT_INDEX_TYPE = "entry"

ENDPOINT_TYPES = (
    "coordinator-http",
    "coordinator-transport",
    "data-http",
    "data-transport",
    "master-http",
    "master-transport",
)
# TODO: similar to DEFAULT_TASK_COUNT, whether or not ingest-http is present is dependent upon
# options.
#    'ingest-http', 'ingest-transport',

DEFAULT_NUMBER_OF_SHARDS = 1
DEFAULT_NUMBER_OF_REPLICAS = 1
DEFAULT_SETTINGS_MAPPINGS = {
    "settings": {
        "index.unassigned.node_left.delayed_timeout": "0",
        "number_of_shards": DEFAULT_NUMBER_OF_SHARDS,
        "number_of_replicas": DEFAULT_NUMBER_OF_REPLICAS,
    },
    "mappings": {
        DEFAULT_INDEX_TYPE: {
            "properties": {"name": {"type": "keyword"}, "role": {"type": "keyword"}}
        }
    },
}


@retrying.retry(
    wait_fixed=1000,
    stop_max_delay=KIBANA_DEFAULT_TIMEOUT * 1000,
    retry_on_result=lambda res: not res,
)
def check_kibana_adminrouter_integration(path: str) -> bool:
    curl_cmd = 'curl -L -I -k -H "Authorization: token={}" -s {}/{}'.format(
        sdk_utils.dcos_token(), sdk_utils.dcos_url().rstrip("/"), path.lstrip("/")
    )
    rc, stdout, _ = sdk_cmd.master_ssh(curl_cmd)
    return bool(rc == 0 and stdout and "HTTP/1.1 200" in stdout)


@retrying.retry(
    wait_fixed=1000, stop_max_delay=DEFAULT_TIMEOUT * 1000, retry_on_result=lambda res: not res
)
def check_elasticsearch_index_health(
    index_name: str,
    color: str,
    service_name: str = SERVICE_NAME,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
    https: bool = False,
) -> bool:
    result = _curl_query(
        service_name,
        "GET",
        "_cluster/health/{}?pretty".format(index_name),
        http_user=http_user,
        http_password=http_password,
        https=https,
    )
    return bool(result and result["status"] == color)


@retrying.retry(wait_fixed=1000, stop_max_delay=5 * 1000, retry_on_result=lambda res: not res)
def check_custom_elasticsearch_cluster_setting(
    service_name: str = SERVICE_NAME,
    setting_path: Optional[str] = None,
    expected_value: Optional[str] = None,
) -> bool:
    settings = _curl_query(service_name, "GET", "_cluster/settings?include_defaults=true")[
        "defaults"
    ]
    if not settings:
        return False
    actual_value = get_in(setting_path, settings)
    log.info(
        "Expected '{}' to be '{}', got '{}'".format(setting_path, expected_value, actual_value)
    )
    return bool(expected_value == actual_value)


@retrying.retry(
    wait_fixed=1000, stop_max_delay=DEFAULT_TIMEOUT * 1000, retry_on_result=lambda res: not res
)
def wait_for_expected_nodes_to_exist(
    service_name: str = SERVICE_NAME, task_count: int = DEFAULT_NODES_COUNT
) -> bool:
    result = _curl_query(service_name, "GET", "_cluster/health")
    if not result or "number_of_nodes" not in result:
        log.warning("Missing 'number_of_nodes' key in cluster health response: {}".format(result))
        return False
    node_count = result["number_of_nodes"]
    log.info("Waiting for {} healthy nodes, got {}".format(task_count, node_count))
    return bool(node_count == task_count)


@retrying.retry(
    wait_fixed=1000, stop_max_delay=DEFAULT_TIMEOUT * 1000, retry_on_result=lambda res: not res
)
def check_kibana_plugin_installed(plugin_name: str, service_name: str = SERVICE_NAME) -> bool:
    task_sandbox = sdk_cmd.get_task_sandbox_path(service_name)
    # Environment variables aren't available on DC/OS 1.9 so we manually inject MESOS_SANDBOX (and
    # can't use ELASTIC_VERSION).
    #
    # TODO(mpereira): improve this by making task environment variables available in task_exec
    # commands on 1.9.
    #
    # Ticket: https://jira.mesosphere.com/browse/INFINITY-3360
    cmd = "bash -c 'KIBANA_DIRECTORY=$(ls -d {}/kibana-*-linux-x86_64); $KIBANA_DIRECTORY/bin/kibana-plugin list'".format(
        task_sandbox
    )
    _, stdout, _ = sdk_cmd.marathon_task_exec(service_name, cmd)
    return bool(plugin_name in stdout)


@retrying.retry(
    wait_fixed=1000, stop_max_delay=DEFAULT_TIMEOUT * 1000, retry_on_result=lambda res: not res
)
def check_elasticsearch_plugin_installed(
    plugin_name: str,
    service_name: str = SERVICE_NAME,
    expected_nodes_count: int = DEFAULT_NODES_COUNT,
) -> bool:
    result = _get_hosts_with_plugin(service_name, plugin_name)
    return result is not None and len(result) == expected_nodes_count


@retrying.retry(
    wait_fixed=1000, stop_max_delay=DEFAULT_TIMEOUT * 1000, retry_on_result=lambda res: not res
)
def check_elasticsearch_plugin_uninstalled(
    plugin_name: str, service_name: str = SERVICE_NAME
) -> bool:
    result = _get_hosts_with_plugin(service_name, plugin_name)
    return result is not None and result == []


def _get_hosts_with_plugin(service_name: str, plugin_name: str) -> Optional[List[str]]:
    output = _curl_query(service_name, "GET", "_cat/plugins", return_json=False)
    if output is None:
        return None
    return [host for host in output.split("\n") if plugin_name in host]


@retrying.retry(wait_fixed=1000, stop_max_delay=120 * 1000, retry_on_result=lambda res: not res)
def get_elasticsearch_master(service_name: str = SERVICE_NAME) -> Optional[str]:
    output = _curl_query(service_name, "GET", "_cat/master", return_json=False)
    assert isinstance(output, str)
    if output is not None and len(output.split()) > 0:
        return output.split()[-1]
    return None


@retrying.retry(wait_fixed=1000, stop_max_delay=30 * 1000, retry_on_result=lambda res: not res)
def verify_graph_explore_endpoint(
    is_expected_to_be_enabled: bool,
    service_name: str = SERVICE_NAME,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> bool:
    index_name = "graph_index"

    create_index(
        index_name,
        DEFAULT_SETTINGS_MAPPINGS,
        service_name=service_name,
        http_user=http_user,
        http_password=http_password,
    )

    query = {
        "query": {"match": {"name": "*"}},
        "vertices": [{"field": "name"}],
        "connections": {"vertices": [{"field": "role"}]},
    }

    response = explore_graph(
        service_name, index_name, query, http_user=http_user, http_password=http_password
    )

    delete_index(
        index_name, service_name=service_name, http_user=http_user, http_password=http_password
    )

    return is_expected_to_be_enabled == is_graph_explore_endpoint_active(response)


def verify_commercial_api_status(
    is_expected_to_be_enabled: bool,
    service_name: str = SERVICE_NAME,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> bool:
    return bool(
        verify_graph_explore_endpoint(
            is_expected_to_be_enabled,
            service_name,
            http_user=http_user,
            http_password=http_password,
        )
    )


# On Elastic 6.x, the "Graph Explore API" is available when the Elasticsearch cluster is configured
# with a "trial" X-Pack license. When configured with a "basic" license (the default) the API will
# respond with an HTTP 403.
#
# A "Graph Explore API" response will look something like:
#   1. With a "trial" license:
#   {
#     "took": 183,
#     "timed_out": false,
#     "failures": [],
#     "vertices": [...],
#     "connections": [...]
#   }
#
#   2. With a "basic" license:
#   {
#     "error": {
#       "root_cause": [
#         {
#           "type": "security_exception",
#           "reason": "current license is non-compliant for [graph]",
#           "license.expired.feature": "graph"
#         }
#       ],
#       "type": "security_exception",
#       "reason": "current license is non-compliant for [graph]",
#       "license.expired.feature": "graph"
#     },
#     "status": 403
#   }
def is_graph_explore_endpoint_active(response: Dict[str, Any]) -> bool:
    return isinstance(response.get("vertices"), list) and isinstance(
        response.get("connections"), list
    )


def verify_document(
    service_name: str,
    document_id: int,
    document_fields: Dict[str, str],
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> None:
    document = get_document(
        DEFAULT_INDEX_NAME,
        DEFAULT_INDEX_TYPE,
        document_id,
        service_name=service_name,
        http_user=http_user,
        http_password=http_password,
    )
    assert document["_source"]["name"] == document_fields["name"]


def get_xpack_license(
    service_name: str = SERVICE_NAME,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> Dict[str, Any]:
    result = _curl_query(
        service_name, "GET", "_xpack/license", http_user=http_user, http_password=http_password
    )
    assert isinstance(result, dict)
    return result


@retrying.retry(wait_fixed=1000, stop_max_delay=120 * 1000, retry_on_result=lambda res: not res)
def verify_xpack_license(
    license_type: str,
    service_name: str = SERVICE_NAME,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> bool:
    response = get_xpack_license(service_name, http_user=http_user, http_password=http_password)

    if "license" not in response:
        log.warning("Missing 'license' key in _xpack/license response: {}".format(response))
        return False  # retry

    assert response["license"]["status"] == "active"
    assert response["license"]["type"] == license_type

    return True  # done


@retrying.retry(
    wait_fixed=1000,
    stop_max_delay=3 * 60 * 1000,
    retry_on_result=lambda return_value: not return_value,
)
def setup_passwords(
    service_name: str = SERVICE_NAME, task_name: str = "master-0-node", https: bool = False
) -> Union[bool, Dict[str, str]]:
    if https:
        master_0_node_dns = sdk_networks.get_endpoint(PACKAGE_NAME, service_name, "master-http")[
            "dns"
        ][0]
        url = "--url https://{}".format(master_0_node_dns)
    else:
        url = ""

    cmd = "\n".join(
        [
            "set -x",
            "export JAVA_HOME=$(ls -d ${MESOS_SANDBOX}/jdk*/)",
            "ELASTICSEARCH_PATH=$(ls -d ${MESOS_SANDBOX}/elasticsearch-*/)",
            "${{ELASTICSEARCH_PATH}}/bin/elasticsearch-setup-passwords auto --batch --verbose {}".format(
                url
            ),
        ]
    )

    full_cmd = "bash -c '{}'".format(cmd)
    _, stdout, _ = sdk_cmd.service_task_exec(service_name, task_name, full_cmd)

    elastic_password_search = re.search("PASSWORD elastic = (.*)", stdout)
    assert isinstance(elastic_password_search, Match)
    elastic_password = elastic_password_search.group(1)

    kibana_password_search = re.search("PASSWORD kibana = (.*)", stdout)
    assert isinstance(kibana_password_search, Match)
    kibana_password = kibana_password_search.group(1)

    if not elastic_password or not kibana_password:
        # Retry.
        return False

    return {"elastic": elastic_password, "kibana": kibana_password}


def explore_graph(
    service_name: str = SERVICE_NAME,
    index_name: str = DEFAULT_INDEX_NAME,
    query: Dict[str, Any] = {},
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> Dict[str, Any]:
    result = _curl_query(
        service_name,
        "POST",
        "{}/_xpack/_graph/_explore".format(index_name),
        json_body=query,
        http_user=http_user,
        http_password=http_password,
    )
    assert isinstance(result, dict)
    return result


def start_trial_license(service_name: str = SERVICE_NAME, https: bool = False) -> Dict[str, Any]:
    result = _curl_query(
        service_name, "POST", "_xpack/license/start_trial?acknowledge=true", https=https
    )
    assert isinstance(result, dict)
    return result


def get_elasticsearch_indices_stats(
    index_name: str, service_name: str = SERVICE_NAME
) -> Dict[str, Any]:
    result = _curl_query(service_name, "GET", "{}/_stats".format(index_name))
    assert isinstance(result, dict)
    return result


def create_index(
    index_name: str,
    params: Dict[str, Any],
    service_name: str = SERVICE_NAME,
    https: bool = False,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> Dict[str, Any]:
    result = _curl_query(
        service_name,
        "PUT",
        index_name,
        json_body=params,
        https=https,
        http_user=http_user,
        http_password=http_password,
    )
    assert isinstance(result, dict)
    return result


def delete_index(
    index_name: str,
    service_name: str = SERVICE_NAME,
    https: bool = False,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> Dict[str, Any]:
    result = _curl_query(
        service_name,
        "DELETE",
        index_name,
        https=https,
        http_user=http_user,
        http_password=http_password,
    )
    assert isinstance(result, dict)
    return result


def create_document(
    index_name: str,
    index_type: str,
    doc_id: int,
    params: Dict[str, str],
    service_name: str = SERVICE_NAME,
    https: bool = False,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> Dict[str, Any]:
    result = _curl_query(
        service_name,
        "PUT",
        "{}/{}/{}?refresh=wait_for".format(index_name, index_type, doc_id),
        json_body=params,
        https=https,
        http_user=http_user,
        http_password=http_password,
    )
    assert isinstance(result, dict)
    return result


def get_document(
    index_name: str,
    index_type: str,
    doc_id: int,
    service_name: str = SERVICE_NAME,
    https: bool = False,
    http_user: Optional[str] = None,
    http_password: Optional[str] = None,
) -> Dict[str, Any]:
    result = _curl_query(
        service_name,
        "GET",
        "{}/{}/{}".format(index_name, index_type, doc_id),
        https=https,
        http_user=http_user,
        http_password=http_password,
    )
    assert isinstance(result, dict)
    return result


def get_elasticsearch_nodes_info(service_name: str = SERVICE_NAME) -> Dict[str, Any]:
    result = _curl_query(service_name, "GET", "_nodes")
    assert isinstance(result, dict)
    return result


# Here we only retry if the command itself failed, or if the data couldn't be parsed as JSON when
# return_json=True. Upstream callers may want to have their own retry loop against the content of
# the returned data (e.g. expected field is missing).
@retrying.retry(wait_fixed=1000, stop_max_delay=120 * 1000, retry_on_result=lambda res: res is None)
def _curl_query(
    service_name: str,
    method: str,
    endpoint: str,
    json_body: Optional[Dict[str, Any]] = None,
    task: str = "master-0-node",
    https: bool = False,
    return_json: bool = True,
    http_user: Optional[str] = DEFAULT_ELASTICSEARCH_USER,
    http_password: Optional[str] = DEFAULT_ELASTICSEARCH_PASSWORD,
) -> Optional[Union[str, Dict[str, Any]]]:
    protocol = "https" if https else "http"

    if http_password:
        if not http_user:
            http_user = DEFAULT_ELASTICSEARCH_USER
            log.info("Using default basic HTTP user: '%s'", http_user)

        credentials = "-u {}:{}".format(http_user, http_password)
    else:
        if http_user:
            raise Exception(
                "HTTP authentication won't work with just a user. Needs both user AND password"
            )
        credentials = ""

    host = sdk_hosts.autoip_host(service_name, task, _master_zero_http_port(service_name))

    curl_cmd = "/opt/mesosphere/bin/curl -sS {} -X{} '{}://{}/{}'".format(
        credentials, method, protocol, host, endpoint
    )

    if json_body:
        json_body_value = json_body if isinstance(json_body, str) else json.dumps(json_body)
        curl_cmd += " -H 'Content-type: application/json' -d '{}'".format(json_body_value)

    task_name = "master-0-node"
    exit_code, stdout, stderr = sdk_cmd.service_task_exec(service_name, task_name, curl_cmd)

    def build_errmsg(msg: str) -> str:
        return "{}\nCommand:\n{}\nstdout:\n{}\nstderr:\n{}".format(msg, curl_cmd, stdout, stderr)

    if exit_code:
        log.warning(
            build_errmsg("Failed to run command on {}, retrying or giving up.".format(task_name))
        )
        return None

    if not return_json:
        return stdout

    try:
        result = json.loads(stdout)
        assert isinstance(result, dict)
        return result
    except Exception:
        log.warning(build_errmsg("Failed to parse stdout as JSON, retrying or giving up."))
        return None


def test_xpack_enabled_update(
    service_name: str,
    from_xpack_enabled: bool,
    to_xpack_enabled: bool,
    from_version: str,
    to_version: str = "stub-universe",
) -> None:
    sdk_upgrade.test_upgrade(
        PACKAGE_NAME,
        service_name,
        DEFAULT_TASK_COUNT,
        from_version=from_version,
        from_options={"elasticsearch": {"xpack_enabled": from_xpack_enabled}},
        to_version=to_version,
        to_options={
            "service": {"update_strategy": "parallel"},
            "elasticsearch": {"xpack_enabled": to_xpack_enabled},
        },
    )

    wait_for_expected_nodes_to_exist(service_name=service_name)


def test_xpack_security_enabled_update(
    service_name: str, from_xpack_security_enabled: bool, to_xpack_security_enabled: bool
) -> None:
    test_upgrade(
        PACKAGE_NAME,
        service_name,
        expected_running_tasks_before_upgrade=DEFAULT_NODES_COUNT,
        expected_running_tasks_after_upgrade=DEFAULT_TASK_COUNT,
        from_options={"elasticsearch": {"xpack_security_enabled": from_xpack_security_enabled}},
        to_options={
            "service": {"update_strategy": "parallel"},
            "elasticsearch": {"xpack_security_enabled": to_xpack_security_enabled},
        },
    )

    wait_for_expected_nodes_to_exist(service_name=service_name)


def test_upgrade_from_xpack_enabled(
    package_name: str,
    service_name: str,
    options: Dict[str, Any],
    expected_task_count: int,
    from_version: str,
    to_version: str = "stub-universe",
) -> None:
    # This test needs to run some code in between the Universe version installation and the upgrade
    # to the 'stub-universe' version, so it cannot use `sdk_upgrade.test_upgrade`.
    http_user = DEFAULT_ELASTICSEARCH_USER
    http_password = DEFAULT_ELASTICSEARCH_PASSWORD

    sdk_install.uninstall(package_name, service_name)

    sdk_install.install(
        package_name,
        service_name,
        expected_running_tasks=expected_task_count,
        additional_options={"elasticsearch": {"xpack_enabled": True}},
        package_version=from_version,
    )

    document_es_5_id = 1
    document_es_5_fields = {"name": "Elasticsearch 5: X-Pack enabled", "role": "search engine"}
    create_document(
        DEFAULT_INDEX_NAME,
        DEFAULT_INDEX_TYPE,
        document_es_5_id,
        document_es_5_fields,
        service_name=service_name,
        http_user=http_user,
        http_password=http_password,
    )

    # This is the first crucial step when upgrading from "X-Pack enabled" on ES5 to "X-Pack security
    # enabled" on ES6. The default "changeme" password doesn't work anymore on ES6, so passwords
    # *must* be *explicitly* set, otherwise nodes won't authenticate requests, leaving the cluster
    # unavailable. Users will have to do this manually when upgrading.
    _curl_query(
        service_name,
        "POST",
        "_xpack/security/user/{}/_password".format(http_user),
        json_body={"password": http_password},
        http_user=http_user,
        http_password=http_password,
    )

    # First we upgrade to "X-Pack security enabled" set to false on ES6, so that we can use the
    # X-Pack migration assistance and upgrade APIs.
    sdk_upgrade.update_or_upgrade_or_downgrade(
        package_name,
        service_name,
        to_version,
        {
            "service": {"update_strategy": "parallel"},
            "elasticsearch": {"xpack_security_enabled": False},
        },
        expected_task_count,
    )

    # Get list of indices to upgrade from here. The response looks something like:
    # {
    #   "indices" : {
    #     ".security" : {
    #       "action_required" : "upgrade"
    #     },
    #     ".watches" : {
    #       "action_required" : "upgrade"
    #     }
    #   }
    # }
    response = _curl_query(service_name, "GET", "_xpack/migration/assistance?pretty")

    # This is the second crucial step when upgrading from "X-Pack enabled" on ES5 to ES6. The
    # ".security" index (along with any others returned by the "assistance" API) needs to be
    # upgraded.
    for index in response["indices"]:
        _curl_query(
            service_name,
            "POST",
            "_xpack/migration/upgrade/{}?pretty".format(index),
            http_user=http_user,
            http_password=http_password,
        )

    document_es_6_security_disabled_id = 2
    document_es_6_security_disabled_fields = {
        "name": "Elasticsearch 6: X-Pack security disabled",
        "role": "search engine",
    }
    create_document(
        DEFAULT_INDEX_NAME,
        DEFAULT_INDEX_TYPE,
        document_es_6_security_disabled_id,
        document_es_6_security_disabled_fields,
        service_name=service_name,
        http_user=http_user,
        http_password=http_password,
    )

    # After upgrading the indices, we're now safe to do the actual configuration update, possibly
    # enabling X-Pack security.
    sdk_service.update_configuration(package_name, service_name, options, expected_task_count)

    document_es_6_post_update_id = 3
    document_es_6_post_update_fields = {
        "name": "Elasticsearch 6: Post update",
        "role": "search engine",
    }
    create_document(
        DEFAULT_INDEX_NAME,
        DEFAULT_INDEX_TYPE,
        document_es_6_post_update_id,
        document_es_6_post_update_fields,
        service_name=service_name,
        http_user=http_user,
        http_password=http_password,
    )

    # Make sure that documents were created and are accessible.
    verify_document(
        service_name,
        document_es_5_id,
        document_es_5_fields,
        http_user=http_user,
        http_password=http_password,
    )
    verify_document(
        service_name,
        document_es_6_security_disabled_id,
        document_es_6_security_disabled_fields,
        http_user=http_user,
        http_password=http_password,
    )
    verify_document(
        service_name,
        document_es_6_post_update_id,
        document_es_6_post_update_fields,
        http_user=http_user,
        http_password=http_password,
    )


def _master_zero_http_port(service_name: str) -> int:
    """Returns a master node hostname+port endpoint that can be queried from within the cluster. We
    cannot cache this value because while the hostnames remain static, the ports are dynamic and may
    change if the master is replaced.

    """
    dns = sdk_networks.get_endpoint(PACKAGE_NAME, service_name, "master-http")["dns"]
    # 'dns' array will look something like this in CCM: [
    #   "master-0-node.[svcname].[...autoip...]:1027",
    #   "master-1-node.[svcname].[...autoip...]:1026",
    #   "master-2-node.[svcname].[...autoip...]:1025"
    # ]

    port = dns[0].split(":")[-1]
    log.info("Extracted {} as port for {}".format(port, dns[0]))
    return int(port)


# Use sdk_upgrade.test_upgrade instead of this function after
# it will be upgraded to accept different number of expecting tasks for install and upgrade
def test_upgrade(
    package_name: str,
    service_name: str,
    expected_running_tasks_before_upgrade: int,
    expected_running_tasks_after_upgrade: int,
    from_version: str = None,
    from_options: Dict[str, Any] = {},
    to_version: str = None,
    to_options: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = sdk_upgrade.TIMEOUT_SECONDS,
    wait_for_deployment: bool = True,
) -> None:
    sdk_install.uninstall(package_name, service_name)

    log.info(
        "Called with 'from' version '{}' and 'to' version '{}'".format(from_version, to_version)
    )

    universe_version = None
    try:
        # Move the Universe repo to the top of the repo list so that we can first install the latest
        # released version.
        test_version, universe_version = sdk_repository.move_universe_repo(
            package_name, universe_repo_index=0
        )
        log.info("Found 'test' version: {}".format(test_version))
        log.info("Found 'universe' version: {}".format(universe_version))

        from_version = from_version or universe_version
        to_version = to_version or test_version

        log.info(
            "Will upgrade {} from version '{}' to '{}'".format(
                package_name, from_version, to_version
            )
        )

        log.info("Installing {} 'from' version: {}".format(package_name, from_version))
        sdk_install.install(
            package_name,
            service_name,
            expected_running_tasks_before_upgrade,
            package_version=from_version,
            additional_options=from_options,
            timeout_seconds=timeout_seconds,
            wait_for_deployment=wait_for_deployment,
        )
    finally:
        if universe_version:
            # Return the Universe repo back to the bottom of the repo list so that we can upgrade to
            # the build version.
            sdk_repository.move_universe_repo(package_name)

    log.info(
        "Upgrading {} from version '{}' to '{}'".format(package_name, from_version, to_version)
    )
    sdk_upgrade.update_or_upgrade_or_downgrade(
        package_name,
        service_name,
        to_version,
        to_options or from_options,
        expected_running_tasks_after_upgrade,
        wait_for_deployment,
        timeout_seconds,
    )
