import openstack
import requests
import logging
import time
import xml.etree.ElementTree as ET
import config

class OpenStackUtils:
    def __init__(self, auth_args):
        self.conn = openstack.connect(**auth_args)

    def get_vm_volumes(self, vm_name):
        try:
            server = self.conn.compute.find_server(vm_name)
            if not server:
                logging.warning(f"[MIGRATION] 未找到名为 {vm_name} 的虚拟机。")
                return []

            volume_attachments = self.conn.compute.get_server(server.id).to_dict().get('attached_volumes', [])
            volume_ids = [attachment['id'] for attachment in volume_attachments]
            volumes = [self.conn.block_storage.get_volume(volume_id) for volume_id in volume_ids]
            return volumes
        except Exception as e:
            logging.error(f"[MIGRATION] 获取虚拟机 {vm_name} 的卷信息时出现错误: {e}")
            return []

    def is_vm_boot_from_volume(self, server):
        """检测虚拟机是否是从卷启动"""
        try:
            # 获取虚拟机详情
            server_details = self.conn.compute.get_server(server.id)
            
            # 如果 image 属性为 None，则表示从卷启动
            return server_details.image is None or server_details.image.get('id') is None
        except Exception as e:
            logging.error(f"[MIGRATION] 检测虚拟机启动方式失败: {e}")
            return False

    def get_vm_status(self, vm_name):
        server = self.conn.compute.find_server(vm_name)
        if server:
            server = self.conn.compute.get_server(server.id)
            return server.status
        return None

    def stop_vm(self, vm_id):
        try:
            server = self.conn.compute.get_server(vm_id)
            if server.status == 'ACTIVE':
                self.conn.compute.stop_server(vm_id)
                logging.info(f"[MIGRATION]正在关闭虚拟机 {vm_id}...")
                # 等待虚拟机状态变为 SHUTOFF
                while True:
                    server = self.conn.compute.get_server(vm_id)
                    if server.status == 'SHUTOFF':
                        logging.info(f"[MIGRATION]虚拟机 {vm_id} 已成功关闭。")
                        break
                    elif server.status in ['ERROR', 'PAUSED', 'SUSPENDED']:
                        logging.error(f"[MIGRATION]虚拟机 {vm_id} 关闭失败，状态为 {server.status}。")
                        break
                    time.sleep(10)
            else:
                logging.info(f"[MIGRATION]虚拟机 {vm_id} 当前状态为 {server.status}，无需关闭。")
        except Exception as e:
            logging.error(f"[MIGRATION]关闭虚拟机 {vm_id} 时出错: {e}")
            
    def get_server_xml(self, server_id):
        try:
            # 获取认证令牌
            token = self.conn.authorize()
            # 获取 Nova API 端点
            nova_endpoint = self.conn.compute.get_endpoint()
            # 构建请求 URL
            url = f"{nova_endpoint}/servers/{server_id}/xml"
            # 设置请求头
            headers = {
                "X-Auth-Token": token,
                "Content-Type": "application/xml"
            }
            # 发送 GET 请求
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            # 返回 XML 描述信息
            return response.text
        except requests.exceptions.RequestException as e:
            logging.error(f"[MIGRATION] 获取虚拟机 {server_id} 的 XML 描述信息时出现错误: {e}")
            return None

    def get_vda_rbd_info(self, vm_name):
        try:
            server = self.get_server_xml(vm_name)
            if not server:
                logging.warning(f"[MIGRATION] 未找到名为 {vm_name} 的虚拟机。")
                return None

            # 获取虚拟机的 XML 描述信息
            xml_desc = self.conn.compute.get_server_xml(server.id)
            root = ET.fromstring(xml_desc)

            # 查找 vda 磁盘设备
            for disk in root.findall('.//disk[@device="disk"]'):
                target = disk.find('target')
                if target is not None and target.get('dev') == 'vda':
                    source = disk.find('source')
                    if source is not None and source.get('type') == 'rbd':
                        pool = source.get('pool')
                        rbd_name = source.get('name')
                        return pool, rbd_name

            logging.warning(f"[MIGRATION] 未找到 {vm_name} 的 vda RBD 磁盘信息。")
            return None
        except Exception as e:
            logging.error(f"[MIGRATION] 获取虚拟机 {vm_name} 的 vda RBD 磁盘信息时出现错误: {e}")
            return None

    def get_vm_network_info(self,conn, server):
        subnet_ids = []
        subnet_info = []
        ports = list(conn.conn.network.ports(device_id=server.id))
        for port in ports:
            for fixed_ip in port.fixed_ips:
                subnet_id = fixed_ip.get('subnet_id')
                if subnet_id:
                    subnet_ids.append({
                        "subnet_id": subnet_id,
                        "ipaddr": fixed_ip["ip_address"]
                        })
        for subnet_id in subnet_ids:
            subnet = conn.conn.network.get_subnet(subnet_id["subnet_id"])
            subnet_info.append({
                    "subnet":subnet,
                    "ipaddr":subnet_id["ipaddr"]
                    })

        return subnet_info 


    def create_port_with_ip(self, network_id,subnet_id, ip_address):
        try:
            port = self.conn.network.create_port(
            name = ip_address,
            network_id=network_id,
            fixed_ips=[{
                "subnet_id": subnet_id,
                "ip_address": ip_address,
                }]
                )
            logging.info(f"[MIGRATION] 成功在网络 {network_id} 中创建端口,IP 地址为 {ip_address}")
            return port
        except Exception as e:
            logging.error(f"[MIGRATION] 在网络 {network_id} 中创建端口,IP 地址为 {ip_address} 时出现错误: {e}")
        return None

    def find_flavor_by_name(self, flavor_name):
        all_flavors = self.conn.compute.flavors()
        for flavor in all_flavors:
            if flavor.name == flavor_name:
                return flavor
        return None

    def volmue_setbootable(self, target_volume):
        token = self.conn.authorize()
        cinder_endpoint = self.conn.block_storage.get_endpoint()
        url = f"{cinder_endpoint}/volumes/{target_volume.id}/action"
        headers = {
            "X-Auth-Token": token,
            "Content-Type": "application/json"
        }
        data = {
            "os-set_bootable": {
                "bootable": True
            }
        }
        try:
            response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            logging.info(f"[MIGRATION] 成功设置卷 {target_volume.id} 为可启动状态。")
        except requests.exceptions.RequestException as e:
            logging.error(f"[MIGRATION] 设置卷可启动状态时出错: {e}")

    def ensure_flavor_exists(self, source_flavor):
        target_flavor = self.find_flavor_by_name(source_flavor.get('original_name'))
        if not target_flavor:
            try:
                target_flavor = self.conn.compute.create_flavor(
                    name=source_flavor.get('original_name'),
                    ram=source_flavor.get("ram"),
                    vcpus=source_flavor.get("vcpus"),
                    disk=source_flavor.get("disk"),
                    ephemeral=source_flavor.get("ephemeral"),
                    swap=source_flavor.get("swap"),
                    #rxtx_factor=source_flavor.rxtx_factor,
                    #is_public=source_flavor.is_public
                )
                logging.info(f"[MIGRATION] 在目标环境中创建 flavor {source_flavor.get('original_name')} 成功。")
            except Exception as e:
                logging.error(f"[MIGRATION] 在目标环境中创建 flavor {source_flavor.get('original_name')} 时出现错误: {e}")
        return target_flavor
    
    def ensure_network_exists(self, source_network):
        result = []
        for target_network in self.conn.network.networks():
            target_subnets = list(self.conn.network.subnets(network_id=target_network.id))
            target_cidrs= [subnet.cidr for subnet in target_subnets]
            for index, cidr in enumerate(target_cidrs):
                #if cidr in source_network.cidr:
                if cidr in source_network[0]["subnet"].cidr:
                    result.append({
                        "network_id": target_network.id,
                        "subnet_id": target_subnets[index].id,
                        "ipaddr":source_network[0]["ipaddr"]
                      })
        return result

    def ensure_security_group_exists(self, source_security_group):
        target_security_group = self.conn.network.find_security_group(source_security_group.get('name'))
        if not target_security_group:
            try:
                target_security_group = self.conn.network.create_security_group(
                    name=source_security_group.name,
                    description=source_security_group.description
                )
                for rule in source_security_group.security_group_rules:
                    self.conn.network.create_security_group_rule(
                        direction=rule['direction'],
                        ethertype=rule['ethertype'],
                        port_range_min=rule.get('port_range_min'),
                        port_range_max=rule.get('port_range_max'),
                        protocol=rule.get('protocol'),
                        remote_ip_prefix=rule.get('remote_ip_prefix'),
                        security_group_id=target_security_group.id
                    )
                logging.info(f"[MIGRATION] 在目标环境中创建安全组 {source_security_group.name} 成功。")
            except Exception as e:
                logging.error(f"[MIGRATION] 在目标环境中创建安全组 {source_security_group.name} 时出现错误: {e}")
        return target_security_group


    def create_vm_in_target(self, vm_name, source_conn, is_boot_from_volume, target_az):
        try:
            # 获取源虚拟机信息
            sources_server = source_conn.conn.compute.find_server(vm_name)
            # 获取源虚拟机的信息
            source_server_info = source_conn.conn.compute.get_server(sources_server.id).to_dict()
            # 获取源虚拟机的网络信息
            source_network=source_conn.get_vm_network_info(source_conn,sources_server)
            # 获取源虚拟机的flavor信息
            source_flavor = source_server_info.get('flavor', {})
            #获取源环境虚拟机镜像信息
            source_image_id = source_server_info.get('image', {}).get('id')
            if source_image_id == None:
                volume_vda = self.conn.block_storage.get_volume(source_server_info.get("attached_volumes")[0].get('id'))
                source_image_id = volume_vda.volume_image_metadata.get("image_id")
            image_name=source_conn.conn.compute.find_image(source_image_id).name
            target_image_id=self.conn.compute.find_image(image_name).id
            if target_image_id == None:
                target_image_id=self.conn.compute.find_image(config.DEFAULT_MIGRATE_IMAGE).id
            #获取源环境硬盘信息
            sources_volumes = source_conn.get_vm_volumes(vm_name)
            # 获取源虚拟机的安全组信息
            source_security_groups = source_server_info.get('security_groups', {})
            target_flavor = self.ensure_flavor_exists(source_flavor)
            target_network = self.ensure_network_exists(source_network)
            #target_security_groups = [self.ensure_security_group_exists(sg) for sg in source_security_groups]

            if not target_flavor or not target_network:
                logging.warning(f"[MIGRATION] 无法在目标环境中创建必要的资源，跳过虚拟机创建。")
                return None

            if sources_volumes[0].is_bootable:
                volume_size=sources_volumes[0].size
            else:
                volume_size=source_flavor.get("disk")

            block_device_mapping = []
            block_device_mapping.append({
              "uuid": target_image_id,
              "boot_index": 0,
              "source_type": "image",
              "volume_type": config.DEFAULT_CINDER_TYPE,
              "volume_size": volume_size,
              "destination_type": "volume",
              "delete_on_termination": False
          })
            for volume in sources_volumes:
                if not volume.is_bootable:
                    block_device_mapping.append({
                        "source_type": "blank",
                        "volume_size": volume.size,
                        "volume_type": config.DEFAULT_CINDER_TYPE,
                        "destination_type": "volume",
                        "delete_on_termination": False
                    })
            # 获取源虚拟机的网络信息
            network_info = target_network
            ports = []
            for info in network_info:
                port = self.create_port_with_ip(info['network_id'],info["subnet_id"], info['ipaddr'])
                if port:
                    ports.append({"port": port.id})

            if not ports:
                logging.warning(f"[MIGRATION] 无法为虚拟机 {vm_name} 创建端口，使用默认网络配置。")
                if network_info:
                    ports = [{"uuid": network_info[0]['network_id']}]
                else:
                    logging.error(f"[MIGRATION] 没有可用的网络信息，无法创建虚拟机。")
                    return None


            server = self.conn.compute.create_server(
                name=vm_name,
                adminPass=config.default_vm_pass,
                flavor_id=target_flavor.id,
                networks=ports,
                block_device_mapping_v2=block_device_mapping,
                #security_groups=[{'name': sg.name} for sg in target_security_groups],
                security_groups=[{"name": config.DEFAULT_SEC_GROUP}],
                availability_zone=target_az  # 指定目标计算可用区
            )

            while True:
                server = self.conn.compute.get_server(server.id)
                if server.status == 'ACTIVE':
                    logging.info(f"[MIGRATION] 虚拟机 {vm_name} 已在目标 OpenStack 环境中创建完成，目标可用区: {target_az}。")
                    self.stop_vm(server.id)
                    break
                elif server.status in ['ERROR', 'PAUSED', 'SUSPENDED']:
                    logging.error(f"[MIGRATION] 虚拟机 {vm_name} 创建失败，状态为 {server.status}，目标可用区: {target_az}。")
                    break
                import time
                time.sleep(5)

            return server
        except Exception as e:
            logging.error(f"[MIGRATION] 在目标 OpenStack 环境中创建虚拟机时出现错误，目标可用区: {target_az}: {e}")
            return None
