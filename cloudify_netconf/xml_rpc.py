# Copyright (c) 2015 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from cloudify import ctx
from cloudify.decorators import operation
from cloudify import exceptions as cfy_exc
from lxml import etree
import netconf_connection
import os
import time
import utils


def _generate_hello(xmlns, netconf_namespace, capabilities):
    """generate initial hello message with capabilities"""
    if not capabilities:
        capabilities = []
    if netconf_connection.NETCONF_1_0_CAPABILITY not in capabilities:
        capabilities.append(netconf_connection.NETCONF_1_0_CAPABILITY)
    if netconf_connection.NETCONF_1_1_CAPABILITY not in capabilities:
        capabilities.append(netconf_connection.NETCONF_1_1_CAPABILITY)
    hello_dict = {
        netconf_namespace + '@capabilities': {
            netconf_namespace + '@capability': capabilities
        }
    }

    hello_xml = utils.generate_xml_node(
        hello_dict,
        xmlns,
        netconf_namespace + '@hello'
    )
    return etree.tostring(
        hello_xml, pretty_print=True, xml_declaration=True,
        encoding='UTF-8'
    )


def _generate_goodbye(xmlns, netconf_namespace, message_id):
    """general final goodbye message"""
    goodbye_dict = {
        netconf_namespace + '@close-session': None,
        '_@@message-id': message_id
    }
    goodbye_xml = utils.generate_xml_node(
        goodbye_dict,
        xmlns,
        netconf_namespace + '@rpc'
    )
    return etree.tostring(
        goodbye_xml, pretty_print=True, xml_declaration=True,
        encoding='UTF-8'
    )


def _server_support_1_1(xmlns, netconf_namespace, response):
    xml_node = etree.XML(response)
    xpath = (
        "/%s:hello/%s:capabilities/%s:capability" % (
            netconf_namespace, netconf_namespace, netconf_namespace
        )
    )
    capabilities = xml_node.xpath(xpath, namespaces=xmlns)
    for node in capabilities:
        xml_dict = {}
        utils.generate_dict_node(
            xml_dict, node,
            xmlns
        )
        value = xml_dict.get(netconf_namespace + '@capability')
        if value == netconf_connection.NETCONF_1_1_CAPABILITY:
            return True
    return False


def _parse_response(xmlns, netconf_namespace, response):
    """parse response from server with check to rpc-error"""
    xml_node = etree.XML(response)
    xml_dict = {}
    utils.generate_dict_node(
        xml_dict, xml_node,
        xmlns
    )
    reply = None
    if 'rpc-reply' in xml_dict:
        reply = xml_dict['rpc-reply']
    elif (netconf_namespace + '@rpc-reply') in xml_dict:
        # default namespace can't be not netconf 1.0
        reply = xml_dict[netconf_namespace + '@rpc-reply']
    if not reply:
        raise cfy_exc.NonRecoverableError(
            "unexpected reply struct"
        )
    # error check
    error = None
    # we can have empty error, so we need additional flag for that
    have_error = False
    if 'rpc-error' in reply:
        error = reply['rpc-error']
        have_error = True
    elif (netconf_namespace + '@rpc-error') in reply:
        # default namespace can't be not netconf 1.0
        error = reply[netconf_namespace + '@rpc-error']
        have_error = True
    if have_error:
        raise cfy_exc.NonRecoverableError(
            "We have error in reply" + str(error)
        )
    return reply


def _merge_ns(base, override):
    """we can have several namespaces in properties and in call"""
    new_ns = {}
    for ns in base:
        new_ns[ns] = base[ns]

    for ns in override:
        new_ns[ns] = override[ns]

    return new_ns


def _gen_relaxng_with_schematron(dsdl, operation=None):
    """generate validation rules by dsdl"""
    # call that will be called without validation
    skiped_actions = [
        "rfc6020@get",
        "rfc6020@get-config",
        "rfc6020@lock",
        "rfc6020@unlock",
        "rfc6020@copy-config"
    ]

    rng_txt = None
    sch_txt = None
    xpath = None
    if not dsdl:
        return rng_txt, sch_txt, xpath
    if operation == "rfc6020@edit-config":
        operation_type = "config"
        xpath = "/rfc6020:rpc/rfc6020:edit-config/rfc6020:config"
    elif operation in skiped_actions:
        return rng_txt, sch_txt, xpath
    else:
        # return rng_txt, sch_txt, xpath
        operation_type = "rpc"
        xpath = "/rfc6020:rpc"

    dsdl = etree.XML(str(dsdl))

    # search base directory, hack for cloudify manager:
    # we have different path to installed package and virtualenv
    virrual_env_path = os.path.dirname(__file__) + "/../../../../"
    if not os.path.isfile(
        virrual_env_path + '/share/netconf/xslt/gen-relaxng.xsl'
    ):
        # more correct way
        # get path from virtual env or use root as base directory
        virrual_env_path = os.environ.get("VIRTUAL_ENV", "/")

    # relaxng xslt
    rng_rpc = open(
        virrual_env_path + '/share/netconf/xslt/gen-relaxng.xsl', 'rb'
    )
    with rng_rpc:
        main_module = operation_type + "-parent"

        xslt_root = etree.parse(rng_rpc)
        transform = etree.XSLT(xslt_root)

        # generate includes for relaxng
        transformed = transform(dsdl, **{
            "schema-dir": "'" + virrual_env_path + "/share/netconf/schema'",
            "gdefs-only": "1"
        })

        # save includes to file dictionary
        # will be used in reintegrate includes to validation
        if operation_type == 'config':
            base_dict = {
                main_module + "-gdefs-" + operation_type + ".rng": etree.XML(
                    str(transformed)
                )
            }
        else:
            base_dict = {
                main_module + "-gdefs.rng": etree.XML(
                    str(transformed)
                )
            }

        # validation for currect action
        transformed = transform(dsdl, **{
            "schema-dir": "'" + virrual_env_path + "/share/netconf/schema'",
            "gdefs-only": "0",
            "target": "'" + operation_type + "'",
            "basename": "'" + main_module + "'"
        })

        # remerge everything
        xmlns = utils.default_xmlns()
        rng = etree.XML(str(transformed))
        utils.load_relaxng_includes(rng, xmlns, base_dict)
        rng_txt = etree.tostring(
            rng, pretty_print=True
        )

    # generate schematron
    sch_rpc = open(
        virrual_env_path + '/share/netconf/xslt/gen-schematron.xsl', 'rb'
    )
    with sch_rpc:
        # generated broken schematron for non config nodes
        if operation_type == 'config':
            xslt_root = etree.parse(sch_rpc)

            transform = etree.XSLT(xslt_root)
            transformed = transform(dsdl, **{
                "schema-dir": (
                    "'" + virrual_env_path + "/share/netconf/schema'"
                ),
                "gdefs-only": "1",
                "target": "'" + operation_type + "'"
            })
            sch_txt = str(transformed)

    return rng_txt, sch_txt, xpath


def _run_one(
    netconf, message_id, operation, netconf_namespace, data, xmlns
):
    """run one call by netconf connection"""
    # rpc
    ctx.logger.info("rpc call")
    parent = utils.rpc_gen(
        message_id, operation, netconf_namespace, data, xmlns
    )

    # send rpc
    rpc_string = etree.tostring(
        parent, pretty_print=True, xml_declaration=True,
        encoding='UTF-8'
    )
    ctx.logger.info("i sent: " + rpc_string)
    response = netconf.send(rpc_string)
    ctx.logger.info("i recieved:" + response)

    response_dict = _parse_response(
        xmlns, netconf_namespace, response
    )
    ctx.logger.info("package will be :" + str(response_dict))
    return response_dict


def _lock(name, lock, netconf, message_id, netconf_namespace, xmlns):
    """lock database by name"""
    operation = "@lock" if lock else "@unlock"
    data = {
        netconf_namespace + "@target": {
            name: {}
        }
    }
    _run_one(
        netconf, message_id, netconf_namespace + operation,
        netconf_namespace, data, xmlns
    )


def _copy(front, back, netconf, message_id, netconf_namespace, xmlns):
    """copy fron database values to back database"""
    data = {
        netconf_namespace + "@source": {
            front: {}
        },
        netconf_namespace + "@target": {
            back: {}
        }
    }
    _run_one(
        netconf, message_id, netconf_namespace + "@copy-config",
        netconf_namespace, data, xmlns
    )


def _update_data(data, operation, netconf_namespace, back):
    """update operation with database name"""
    if operation != 'rfc6020@edit-config':
        return
    if "target" in data:
        if not data["target"]:
            data["target"] = {back: None}
        return
    if netconf_namespace + "@target" in data:
        if data[netconf_namespace + "@target"]:
            return
    data[netconf_namespace + "@target"] = {back: None}


@operation
def run(**kwargs):
    """main entry point for all calls"""

    calls = kwargs.get('calls', [])
    if not calls:
        ctx.logger.info("No calls")
        return

    # credentials
    properties = ctx.node.properties
    ip = properties.get('netconf_auth', {}).get('ip')
    user = properties.get('netconf_auth', {}).get('user')
    password = properties.get('netconf_auth', {}).get('password')
    key_content = properties.get('netconf_auth', {}).get('key_content')
    port = properties.get('netconf_auth', {}).get('port', 830)
    if not ip or not user or (not password and not key_content):
        raise cfy_exc.NonRecoverableError(
            "please check your credentials"
        )

    # some random initial message id, for have different between calls
    message_id = int((time.time() * 100) % 100 * 1000)

    # xml namespaces and capabilities
    xmlns = properties.get('metadata', {}).get('xmlns', {})

    # override by system namespaces
    xmlns = _merge_ns(xmlns, properties.get('base_xmlns', {}))

    netconf_namespace, xmlns = utils.update_xmlns(
        xmlns
    )
    capabilities = properties.get('metadata', {}).get('capabilities')

    # connect
    ctx.logger.info("use %s@%s for login" % (user, ip))
    hello_string = _generate_hello(
        xmlns, netconf_namespace, capabilities
    )
    ctx.logger.info("i sent: " + hello_string)
    netconf = netconf_connection.connection()
    capabilities = netconf.connect(
        ip, user, hello_string, password, key_content, port
    )
    ctx.logger.info("i recieved: " + capabilities)

    if _server_support_1_1(xmlns, netconf_namespace, capabilities):
        ctx.logger.info("i will use version 1.1 of netconf protocol")
        netconf.current_level = netconf_connection.NETCONF_1_1_CAPABILITY

    if 'lock' in kwargs:
        message_id = message_id + 1
        for name in kwargs['lock']:
            _lock(
                name, True, netconf, message_id, netconf_namespace,
                xmlns
            )

    if 'back_database' in kwargs and 'front_database' in kwargs:
        message_id = message_id + 1
        _copy(
            kwargs['front_database'], kwargs['back_database'],
            netconf, message_id, netconf_namespace, xmlns
        )

    # recheck before real send
    for call in calls:
        operation = call.get('action')
        if not operation:
            continue
        data = call.get('payload', {})

        # gen xml for check
        parent = utils.rpc_gen(
            message_id, operation, netconf_namespace, data, xmlns
        )

        # try to validate
        validate_xml = call.get('validate_xml', True)

        if validate_xml:

            # validate rpc
            rng, sch, xpath = _gen_relaxng_with_schematron(
                properties.get('metadata', {}).get('dsdl'),
                call.get('action')
            )
        else:
            xpath = None

        if xpath:
            ctx.logger.info(
                "We have some validation rules for '{}'".format(
                    str(xpath)
                )
            )

            utils.xml_validate(
                parent, xmlns, xpath, rng, sch
            )

    # we can have several calls in one session,
    # like lock, edit-config, unlock
    for call in calls:
        operation = call.get('action')
        if not operation:
            ctx.logger.info("No operations")
            continue
        data = call.get('payload', {})

        message_id = message_id + 1

        if "@" not in operation:
            operation = "_@" + operation

        _update_data(
            data, operation, netconf_namespace,
            kwargs.get('back_database')
        )

        response_dict = _run_one(
            netconf, message_id, operation, netconf_namespace, data,
            xmlns
        )

        # save results to runtime properties
        save_to = call.get('save_to')
        if save_to:
            ctx.instance.runtime_properties[save_to] = response_dict
            ctx.instance.runtime_properties[save_to + "_ns"] = xmlns

    if 'back_database' in kwargs and 'front_database' in kwargs:
        message_id = message_id + 1
        _copy(
            kwargs['back_database'], kwargs['front_database'],
            netconf, message_id, netconf_namespace, xmlns
        )

    if 'lock' in kwargs:
        message_id = message_id + 1
        for name in kwargs['lock']:
            _lock(
                name, False, netconf, message_id, netconf_namespace,
                xmlns
            )

    # goodbye
    ctx.logger.info("connection close")
    message_id = message_id + 1
    goodbye_string = _generate_goodbye(
        xmlns, netconf_namespace, message_id
    )
    ctx.logger.info("i sent: " + goodbye_string)

    response = netconf.close(goodbye_string)
    ctx.logger.info("i recieved: " + response)
