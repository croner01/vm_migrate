import pandas as pd
import logging
from openstack_utils import OpenStackUtils
from ceph_utils import CephUtils
import concurrent.futures

class MigrationManager:
    def __init__(self, source_auth_args, target_auth_args, source_ceph_conf, source_ceph_pool, target_ceph_conf, target_ceph_pool):
        self.source_auth_args = source_auth_args
        self.target_auth_args = target_auth_args
        self.source_ceph_conf = source_ceph_conf
        self.source_ceph_pool = source_ceph_pool
        self.target_ceph_conf = target_ceph_conf
        self.target_ceph_pool = target_ceph_pool

    def find_vm_by_ip(self, source_conn, ip_address):
        servers = source_conn.conn.compute.servers()
        for server in servers:
            for network in server.addresses.values():
                for ip in network:
                    if ip['addr'] == ip_address:
                        return server
        return None
    
    def migrate_vm_cross_openstack_ceph(self, vm_name, concurrency, target_az, migration_method):
        # 为每个线程创建独立的连接
        source_conn = OpenStackUtils(self.source_auth_args)
        target_conn = OpenStackUtils(self.target_auth_args)
        ceph_utils = CephUtils(self.source_ceph_conf, self.source_ceph_pool, self.target_ceph_conf, self.target_ceph_pool)
        try:
            
            
            #获取源环境的虚拟机信息
            sources_server = source_conn.conn.compute.find_server(vm_name)
            #源环境的虚拟机启动方式为boot from image 还是boot from volume
            is_boot_from_volume = source_conn.is_vm_boot_from_volume(sources_server)
            # 获取虚拟机的所有卷
            sources_volumes = source_conn.get_vm_volumes(vm_name)
            # 在目标 OpenStack 环境中创建虚拟机,返回创建好的虚拟机信息
            if migration_method == 'snapshot' or migration_method == 'full_migrate':
                create_target_vm = target_conn.create_vm_in_target(vm_name, source_conn, is_boot_from_volume, target_az)
                if not create_target_vm:
                    logging.error(f"[MIGRATION] 虚拟机 {vm_name} 创建创建失败: {e}")
                    return
            target_volumes = target_conn.get_vm_volumes(vm_name+"2")
            #rbd同步源目标端数据
            ceph_utils.create_volumes_in_target(sources_server, sources_volumes, target_volumes, is_boot_from_volume, concurrency, migration_method)
        except Exception as e:
            logging.error(f"[MIGRATION] 迁移虚拟机 {vm_name} 时出现错误: {e}")
            return vm_name
    def batch_migrate_from_excel(self, file_path, concurrency, migration_method):
        try:
            error_vms = []
            df = pd.read_excel(file_path)
            vm_names = df['vm_name'].dropna().tolist()
            target_azs = df['target_az'].dropna().tolist()
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = []
                for vm_name, target_az in zip(vm_names, target_azs):
                    logging.info(f"[MIGRATION] 开始处理虚拟机 {vm_name} 的迁移任务，目标可用区: {target_az}")
                    future = executor.submit(self.migrate_vm_cross_openstack_ceph, vm_name, concurrency, target_az, migration_method)
                    futures.append(future)

                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        error_vms.append(result)

            if error_vms:
                logging.warning(f"[MIGRATION]以下虚拟机迁移出错: {', '.join(error_vms)}")
        except FileNotFoundError:
            logging.error(f"[MIGRATION] 未找到 Excel 文件: {file_path}")
        except Exception as e:
            logging.error(f"[MIGRATION] 处理 Excel 文件时出现错误: {e}")
