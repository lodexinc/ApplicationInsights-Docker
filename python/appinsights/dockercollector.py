#
# ApplicationInsights-Docker
# Copyright (c) Microsoft Corporation
# All rights reserved.
#
# MIT License
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the ""Software""), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
#

__author__ = 'galha'

import concurrent.futures
import time
import dateutil.parser
from appinsights.dockerwrapper import DockerWrapperError
from appinsights import dockerconvertors


class DockerCollector(object):
    """ The application insights docker collector,
    used to collect data from the docker remote API (events, and performance counters)
    """

    _cmd_template = "/bin/sh -c \"[ -f {file} ] && cat {file}\""

    def _default_print(text):
        print(text, flush=True)

    def __init__(self, docker_wrapper, docker_injector, samples_in_each_metric=2, send_event=_default_print,
                 sdk_file='/usr/appinsights/docker/sdk.info'):
        """ Initializes a new instance of the class.

        :param docker_wrapper: A docker client wrapper instance
        :param docker_injector: A docker docker injector instance
        :param samples_in_each_metric: The Number of samples to use in each metric
        :param send_event: Function to send event
        :param sdk_file: The sdk file location
        :return:
        """
        super().__init__()
        assert docker_wrapper is not None, 'docker_client cannot be None'
        assert docker_injector is not None, 'docker_injector cannot be None'
        assert samples_in_each_metric > 1, 'samples_in_each_metric must be greater than 1, given: {0}'.format(
            samples_in_each_metric)
        self._sdk_file = sdk_file
        self._docker_wrapper = docker_wrapper
        self._docker_injector = docker_injector
        self._samples_in_each_metric = samples_in_each_metric
        self._send_event = send_event
        self._my_container_id = None
        self._containers_state = {}

    def collect_stats_and_send(self):
        """
        Collects docker metrics from docker and sends them to sender
        cpu, memory, rx_bytes ,tx_bytes, blkio metrics
        """

        if self._my_container_id is None:
            self._my_container_id = self._docker_injector.get_my_container_id()

        host_name = self._docker_wrapper.get_host_name()
        containers = self._docker_wrapper.get_containers()
        self._update_containers_state(containers=containers)
        containers_without_sdk = [v['container'] for k, v in self._containers_state.items() if
                                  k == self._my_container_id or v['ikey'] is None]

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(containers), 30)) as executor:
            container_stats = list(
                executor.map(
                    lambda container: (container, self._docker_wrapper.get_stats(container=container,
                                                                                 stats_to_bring=self._samples_in_each_metric)),
                    containers_without_sdk))

        for container, stats in [(container, stats) for container, stats in container_stats if len(stats) > 1]:
            metrics = dockerconvertors.convert_to_metrics(stats)
            properties = dockerconvertors.get_container_properties(container, host_name)
            for metric in metrics:
                self._send_event({'metric': metric, 'properties': properties})

    def collect_container_events(self):
        """ Collects the container events (start, stop, die, pause, unpause)
        and sends then using the send_event function given in the constructor
        :return:
        """
        event_name_template = 'docker-container-{0}'
        host_name = self._docker_wrapper.get_host_name()
        for event in self._docker_wrapper.get_events():
            status = event['status']
            if status not in ['start', 'stop', 'die', 'restart', 'pause', 'unpause']:
                continue

            event_name = event_name_template.format(status)
            inspect = self._docker_wrapper.get_inspection(event)
            properties = dockerconvertors.get_container_properties_from_inspect(inspect, host_name)

            ikey_to_send_event = self._get_container_sdk_ikey_from_containers_state(properties['Docker container id'])

            properties['docker-status'] = status
            properties['docker-Created'] = inspect['Created']
            properties['docker-StartedAt'] = inspect['State']['StartedAt']
            properties['docker-RestartCount'] = inspect['RestartCount']

            if status in ['stop', 'die']:
                properties['docker-FinishedAt'] = inspect['State']['FinishedAt']
                properties['docker-ExitCode'] = inspect['State']['ExitCode']

                error = inspect['State']['Error']
                properties['docker-Error'] = error if (error is not None) else ""
                duration = dateutil.parser.parse(properties['docker-FinishedAt']) - dateutil.parser.parse(
                    properties['docker-StartedAt'])
                duration_seconds = duration.total_seconds()
                properties['docker-duration-seconds'] = duration_seconds
                properties['docker-duration-minutes'] = duration_seconds / 60
                properties['docker-duration-hours'] = duration_seconds / 3600
                properties['docker-duration-days'] = duration_seconds / 86400
            event_data = {'name': event_name, 'ikey': ikey_to_send_event if ikey_to_send_event is not None else '', 'properties': properties}
            self._send_event(event_data)

    @staticmethod
    def remove_old_containers(current_containers, new_containers):
        """
            This function removes all old containers that have been stopped.

            :param current_containers: The containers currently in cache.
            :param new_containers: The latest containers collection.
            :rtype : dict
            """
        curr_containers_ids = {c['Id']: c for c in new_containers}
        keys = [k for k in current_containers]
        for key in [key for key in keys if key not in curr_containers_ids]:
            if current_containers[key]['unregistered'] is None:
                current_containers[key]['unregistered'] = time.time()
            else:
                if current_containers[key]['unregistered'] < time.time() - 60:
                    del current_containers[key]

        return current_containers

    def _get_container_sdk_info(self, container):
        try:
            result = self._docker_wrapper.run_command(container,
                                                      DockerCollector._cmd_template.format(file=self._sdk_file))
            result = result.strip()

            return result if result != '' else None
        except DockerWrapperError:
            return None

    def _get_container_sdk_ikey_from_containers_state(self, container_id):
        if container_id not in self._containers_state.keys():
            containers = self._docker_wrapper.get_containers()
            self._update_containers_state(containers=containers)

        if container_id in self._containers_state.keys():
            return self._containers_state[container_id]['ikey']
        else:
            return None

    def _update_containers_state(self, containers):
        self._containers_state = DockerCollector.remove_old_containers(self._containers_state, containers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(containers), 30)) as executor:
            list(executor.map(lambda c: self._update_container_state(c), containers))

    def _update_container_state(self, container):
        id = container['Id']
        if id not in self._containers_state:
            for i in range(5):
                ikey = self._get_container_sdk_ikey(container)
                self._containers_state[id] = {'ikey': ikey, 'registered': time.time(), 'unregistered': None, 'container': container}

                if ikey is not None:
                    return ikey

                time.sleep(1)

            return None

        status = self._containers_state[id]
        if status['ikey'] is not None:
            return status['ikey']

        if status['registered'] > time.time() - 60:
            ikey = self._get_container_sdk_ikey(container)
            status['ikey'] = ikey
            return ikey

        return None

    def _get_container_sdk_ikey(self, container):
        sdk_info_file_content = self._get_container_sdk_info(container)
        if sdk_info_file_content is None:
            return None
        splits = sdk_info_file_content.split('=')
        if len(splits) < 2:
            return None
        return sdk_info_file_content.split('=')[1]