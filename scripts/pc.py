#!/usr/bin/env python

import json
import psutil
import netifaces
from docker import Client

import rospy
from std_msgs.msg import Empty
import diagnostic_updater
import diagnostic_msgs

from pioneer_outdoor.msg import DockerContainer, DockerContainers
from pioneer_outdoor.srv import DockerContainer
from pioneer_outdoor.srv import DockerSetRestartContainer


class PC(object):

    def set_restart_policy(self, name, policy, max_retry):
        return "Not implemented"

    def init_docker_info(self):
        containers = self.cli.containers()
        self.containers = [{'stats': self.cli.stats(c['Id']),
                            'id':c['Id'],
                            'name':c['Names'][0]}
                           for c in containers]
        self.docker_seq = 1

    def update_docker_info(self, event):

        msgs = DockerContainers()
        msgs.header.stamp = rospy.Time.now()
        msgs.header.seq = self.docker_seq
        self.docker_seq = self.docker_seq + 1
        for c in self.containers:
            msg = DockerContainer()
            msg.name = c['name']
            try:
                data = json.loads(next(c['stats']))
            except StopIteration:
                # TODO complete
                continue 
            # Get start date (string, e.g. 2016-05-10T13:42:43.332441784Z) with
            # state['StartedAt']
            memory = data['memory_stats']
            c['memory_usage'] = memory['usage'] / 1e6
            c['memory_limit'] = memory['limit'] / 1e6
            cpu = data["cpu_stats"]
            precpu = data["precpu_stats"]
            cpu_delta = cpu["cpu_usage"]["total_usage"] - \
                precpu["cpu_usage"]["total_usage"]
            system_delta = cpu["system_cpu_usage"] - precpu["system_cpu_usage"]
            if system_delta > 0 and cpu_delta > 0:
                c['cpu'] = cpu_delta / \
                    float(system_delta) * len(cpu["cpu_usage"]["percpu_usage"])

            msg.cpu = c['cpu']
            msg.memory_limit = c['memory_limit']
            msg.memory_usage = c['memory_usage']

            inspect = self.cli.inspect_container(c['name'])
            state = inspect['State']
            c['status'] = state['Status']
            msg.status = c['status']
            config = inspect['HostConfig']
            restart = config['RestartPolicy']

            msg.restart_policy = restart['Name']
            msg.restart_max_retry = restart['MaximumRetryCount']

            msgs.containers.append(msg)

        self.docker_pub.publish(msgs)

    def docker_diagnostics(self, container):
        c_stats = self.cli.stats(container)

        def update(stats):
            stats.summary(diagnostic_msgs.msg.DiagnosticStatus.OK, "Running")
            data = json.loads(next(c_stats))
            mem_u = data['memory_stats']['usage']
            mem_t = data['memory_stats']['limit']
            stats.add("memory", "%.3g | %.0g  " % (mem_u / 1e6, mem_t / 1e6))
            stats.add("memory percent", "%.2g%%" %
                      (100 * mem_u / 1e6 / mem_t / 1e6))
            cpu_usage = data["cpu_stats"]["cpu_usage"]["total_usage"]
            precpu_usage = data["precpu_stats"]["cpu_usage"]["total_usage"]
            system_cpu_usage = data["cpu_stats"]["system_usage"]
            system_precpu_usage = data["precpu_stats"]["system_usage"]
            cpu_delta = cpu_usage - precpu_usage
            system_delta = system_cpu_usage - system_precpu_usage
            if system_delta > 0 and cpu_delta > 0:
                cpu_percent = 100.0 * cpu_delta / system_delta
                stats.add("cpu" "%.2g%%" % cpu_percent)
            return stats
        return update

    def cpu_diagnostics(self, stat):
        cpu = psutil.cpu_percent()
        if cpu > 80:
            stat.summary(diagnostic_msgs.msg.DiagnosticStatus.WARN,
                         "Using almost all cpu")
        else:
            stat.summary(diagnostic_msgs.msg.DiagnosticStatus.OK, "Ok")
        stat.add("cpu percent", cpu)
        return stat

    def memory_diagnostics(self, stat):
        mem = psutil.phymem_usage()
        if mem.percent > 80:
            stat.summary(diagnostic_msgs.msg.DiagnosticStatus.WARN,
                         "Using almost all memory")
        else:
            stat.summary(diagnostic_msgs.msg.DiagnosticStatus.OK, "Ok")
        stat.add("memory percent", mem.percent)
        stat.add("memory usage (MB)", int(mem.used / 1e6))
        return stat

    def network_diagnostics(self, iface):
        def update(stat):
            if iface not in netifaces.interfaces():
                stat.summary(diagnostic_msgs.msg.DiagnosticStatus.ERROR,
                             "%s is not defined" % iface)
                return stat
            ip = netifaces.ifaddresses(iface)[2]
            if not ip or not ip[0]['addr']:
                stat.summary(diagnostic_msgs.msg.DiagnosticStatus.ERROR,
                             "%s is down, no ip assigned" % iface)
                return stat
            net = psutil.network_io_counters(pernic=True).get(iface)
            if net.errin or net.errout:
                stat.summary(
                    diagnostic_msgs.msg.DiagnosticStatus.ERROR,
                    "Some error packets")
            elif net.dropin or net.dropout:
                stat.summary(diagnostic_msgs.msg.DiagnosticStatus.WARN,
                             "Some packets have been dropped")
            else:
                stat.summary(diagnostic_msgs.msg.DiagnosticStatus.OK, "Ok")
            for k, v in net._asdict().items():
                stat.add(k, v)
        return update

    def update_diagnostics(self, event):
        self.updater.update()

    def __init__(self):

        rospy.init_node('pc_controller', anonymous=False)

        self.cli = Client(
            base_url='unix://var/run/docker.sock', version='1.22')
        self.updater = diagnostic_updater.Updater()
        self.updater.setHardwareID("macmini")
        self.updater.add("CPU", self.cpu_diagnostics)
        self.updater.add("Memory", self.memory_diagnostics)
        for iface in ['wlan0', 'eth0']:
            self.updater.add("Network %s" %
                             iface, self.network_diagnostics(iface))

        # for container in ['romantic_hypatia','high_engelbart']:
        #    self.updater.add("Docker %s" % container,
        #                     self.docker_diagnostics(container))

        rospy.Subscriber("shutdown", Empty, self.has_received_shutdown)
        rospy.Subscriber("reboot", Empty, self.has_received_reboot)

        self.docker_pub = rospy.Publisher(
            'docker/containers', DockerContainers, queue_size=1)
        self.init_docker_info()

        rospy.Timer(rospy.Duration(1), self.update_docker_info, oneshot=False)
        rospy.Timer(rospy.Duration(1), self.update_diagnostics, oneshot=False)


        for cmd in ['start','stop','pause','unpause']:
            rospy.Service('docker/{cmd}'.format(cmd=cmd), DockerContainer,
                          lambda request: self.cli.__getattribute__(cmd)(request.name) or "{cmd} {name} done!".format(cmd=cmd, name=request.name)
        
        rospy.Service('docker/restart_policy', DockerSetRestartContainer,
                      lambda request: self.set_restart_policy(
                          request.name, request.policy, request.max_retry))

        rospy.spin()

    def has_received_reboot(self, msg):
        rospy.loginfo("Begin reboot")

    def has_received_shutdown(self, msg):
        rospy.loginfo("Begin shutdown")

if __name__ == '__main__':
    try:
        PC()
    except rospy.ROSInterruptException:
        rospy.loginfo("Controller finished.")