import subprocess
import logging
import concurrent.futures
from datetime import datetime

class CephUtils:
    def __init__(self, source_ceph_conf, source_ceph_pool, target_ceph_conf, target_ceph_pool):
        self.source_ceph_conf = source_ceph_conf
        self.source_ceph_pool = source_ceph_pool
        self.target_ceph_conf = target_ceph_conf
        self.target_ceph_pool = target_ceph_pool
        
    def _run_command(self, command, error_message):
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as e:
            logging.error(f"[MIGRATION] {error_message}: {e}")
            return None    

    def get_latest_snapshot(self, source_ceph_conf, source_ceph_pool, source_rbd_id):
        """
        获取指定 RBD 镜像的最新快照
        :param source_ceph_conf: 源 Ceph 配置文件路径
        :param source_ceph_pool: 源 RBD 池名称
        :param source_rbd_id: 源 RBD 镜像 ID
        :return: 最新快照名称，如果没有快照则返回 None
        """
        try:
            # 执行 rbd snap ls 命令获取所有快照信息
            command = f'rbd --conf {source_ceph_conf} snap ls {source_ceph_pool}/{source_rbd_id}'
            output = self._run_command(command, "执行 rbd snap ls 命令时出错")

            # 解析输出，提取快照名称和创建时间
            snapshots = []
            lines = output.strip().split('\n')[1:]  # 跳过标题行
            for line in lines:
                parts = line.split()
                if len(parts) >= 4:
                    snapshot_name = parts[1]
                    creation_time_str = ' '.join(parts[-5:])
                    import re
                    clean_time_str = re.sub(r'[^a-zA-Z0-9: ]', '', creation_time_str)
                    # 这里简单假设时间格式可以被解析
                    try:
                        creation_time = int(subprocess.run(f"date -d '{clean_time_str}' +%s", shell=True, capture_output=True, text=True).stdout.strip())
                        snapshots.append((snapshot_name, creation_time))
                    except ValueError:
                        logging.warning(f"[MIGRATION] 无法解析快照 {snapshot_name} 的创建时间: {clean_time_str}")

            if snapshots:
                # 按创建时间降序排序
                snapshots.sort(key=lambda x: x[1], reverse=True)
                latest_snapshot = snapshots[0][0] if len(snapshots) > 0 else None
                return latest_snapshot
        except subprocess.CalledProcessError as e:
            logging.error(f"[MIGRATION] 执行 rbd snap ls 命令时出错: {e}")
        return None

    def sync_from_latest_snapshot(self, source_ceph_conf, source_ceph_pool, source_rbd_id, target_ceph_conf, target_ceph_pool, target_rbd_id, rbd_name):
        """
        从源 RBD 镜像的最新快照进行数据同步到目标 RBD 镜像
        :param source_ceph_conf: 源 Ceph 配置文件路径
        :param source_ceph_pool: 源 RBD 池名称
        :param source_rbd_id: 源 RBD 镜像 ID
        :param target_ceph_conf: 目标 Ceph 配置文件路径
        :param target_ceph_pool: 目标 RBD 池名称
        :param target_rbd_id: 目标 RBD 镜像 ID
        :return: 同步是否成功
        """
        latest_latest_snapshot = self.get_latest_snapshot(source_ceph_conf, source_ceph_pool, source_rbd_id)
        try:
            #快照回滚
            rollback_command = [
                "rbd",
                "--conf", target_ceph_conf,
                "snap",
                "rollback",
                f"{target_ceph_pool}/{target_rbd_id}@{latest_latest_snapshot}"
            ]
            self._run_command(" ".join(rollback_command), "执行出错")
            logging.info("[MIGRATION] 快照回滚成功。")

	    # 执行 rbd diff 并同步
            diff_command = [
                "rbd",
                "--conf", source_ceph_conf,
                "export-diff",
                f"{source_ceph_pool}/{source_rbd_id}",
                "--from-snap", latest_latest_snapshot,
                "-",
                "|",
                "rbd",
                "--conf", target_ceph_conf,
                "import-diff",
                "-",
                f"{target_ceph_pool}/{target_rbd_id}"
            ]
            result = self._run_command(" ".join(diff_command), "执行出错")
            logging.info("[MIGRATION] 数据同步成功。")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"[MIGRATION] 数据同步失败: {e}")
            return False

    
    def create_rbd_snapshot(self, rbd_pool, source_rbd_id, rbd_name):
        try:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            snapshot_name = f"{rbd_name}-snapshot-{timestamp}"
            command = [
                "rbd",
                "--conf", self.source_ceph_conf,
                "snap", "create",
                f"{rbd_pool}/{source_rbd_id}@{snapshot_name}"
            ]
            self._run_command(" ".join(command), "执行出错")
            logging.info(f"[MIGRATION] 为 RBD 卷 {rbd_pool}/{rbd_name} 创建快照 {snapshot_name} 成功。")
            return snapshot_name
        except subprocess.CalledProcessError as e:
            logging.error(f"[MIGRATION] 为 RBD 卷 {rbd_pool}/{rbd_name} 创建快照时出错: {e}")
            return None

    def migrate_rbd_data_from_snapshot(self, source_rbd_pool, source_rbd_id, snapshot_name, target_rbd_pool, target_rbd_id, volume_size):
        try:
            #rbd rm删除目标卷
            rm_command = f"rbd --conf {self.target_ceph_conf} rm {target_rbd_pool}/{target_rbd_id}"
            self._run_command(rm_command, "执行出错")
            logging.info(f"[MIGRATION]{target_rbd_pool}/{target_rbd_id}删除成功 。")
            #rbd 重建目标卷，采用8M的块
            rebuild_command=f"rbd --conf {self.target_ceph_conf} create --size {volume_size} {target_rbd_pool}/{target_rbd_id} --object-size 4M"
            self._run_command(rebuild_command, "执行出错")
            logging.info(f"[MIGRATION]{target_rbd_pool}/{target_rbd_id}重建成功 。")
            command = [
                "rbd",
                "--conf", self.source_ceph_conf,
                "export-diff",
                f"{source_rbd_pool}/{source_rbd_id}@{snapshot_name}",
                "-",
                "|",
                "rbd",
                "--conf", self.target_ceph_conf,
                "import-diff",
                "-",
                f"{target_rbd_pool}/{target_rbd_id}"
            ]
            # 由于管道的复杂性，这里使用 shell 模式执行命令
            self._run_command(" ".join(command), "执行出错")
            logging.info(f"[MIGRATION] 从快照 {snapshot_name} 迁移 RBD 卷 {source_rbd_pool}/{source_rbd_id} 到目标 {target_rbd_pool}/{target_rbd_id} 成功。")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"[MIGRATION] 从快照 {snapshot_name} 迁移 RBD 卷 {source_rbd_pool}/{source_rbd_id} 到目标 {target_rbd_pool}/{target_rbd_id} 时出错: {e}")
            return False

    def full_migrate_rbd_volume(self, source_rbd_pool, rbd_name, source_rbd_id, target_rbd_pool, target_rbd_id):
            try:
                rm_command = f"rbd --conf {self.target_ceph_conf} rm {target_rbd_pool}/{target_rbd_id}"
                self._run_command(rm_command, "执行出错")
                export_import_command = [
                    "rbd",
                    "--conf", self.source_ceph_conf,
                    "export",
                    f"{source_rbd_pool}/{source_rbd_id}",
                    "-",
                    "|",
                    "rbd",
                    "--conf", self.target_ceph_conf,
                    "import",
                    "-",
                    f"{target_rbd_pool}/{target_rbd_id}"
                ]
                result = self._run_command(" ".join(export_import_command), "执行出错")
                if result != None:
                    logging.info(f"[MIGRATION] {rbd_name} 数据完整迁移成功")
                    return True
                else:
                    logging.error(f"[MIGRATION] {rbd_name} 数据完整迁移失败")
                    return False
            except Exception as e:
                logging.error(f"[MIGRATION] 执行 rbd export import 时出错: {e}")
                return False

    def migrate_rbd_data(self, source_rbd_pool, rbd_name, source_rbd_id, target_rbd_pool, target_rbd_id, volume_size, migration_method):
        if migration_method == 'snapshot':
            # 创建快照
            snapshot_name = self.create_rbd_snapshot(source_rbd_pool, source_rbd_id, rbd_name)
            if snapshot_name is None:
                return False
            # 从快照迁移数据
            if not self.migrate_rbd_data_from_snapshot(source_rbd_pool, source_rbd_id, snapshot_name, target_rbd_pool, target_rbd_id, volume_size):
                return False
            logging.info(f"[MIGRATION] {rbd_name} 数据迁移成功")
            return True
        elif migration_method == 'rbd_diff':
            # 直接进行 RBD diff 数据同步
            if not self.sync_from_latest_snapshot(self.source_ceph_conf, source_rbd_pool, source_rbd_id, self.target_ceph_conf, target_rbd_pool, target_rbd_id, rbd_name):
                return False
            logging.info(f"[MIGRATION] {rbd_name} 数据同步成功")
            return True
        elif migration_method == 'full_migrate':
            # 直接进行 RBD 完整卷导入导出
            if not self.full_migrate_rbd_volume(source_rbd_pool, rbd_name, source_rbd_id, target_rbd_pool, target_rbd_id):
                return False
            logging.info(f"[MIGRATION] {rbd_name} 数据迁移成功")
            return True
        else:
            logging.error(f"[MIGRATION] 不支持的迁移方式: {migration_method}")
            return False

    def create_volumes_in_target(self,sources_server, sources_volumes, target_volumes, is_boot_from_volume, concurrency, migration_method):
        if is_boot_from_volume:
            sources_all_volumes_info = [
                {
                    "name": volume.name,
                    "volume_id": "volume-" + volume.id,
                    "is_bootable": volume.is_bootable,
                    "size": volume.size,
                    "pool": self.source_ceph_pool,
                }
                for volume in sources_volumes
            ]
        else:
            sources_all_volumes_info = [
                {
                    "name": f"{sources_server.name}_vda",
                    "volume_id": f"{sources_server.id}_disk",
                    "is_bootable": True,
                    "size": None,
                    "pool": "vms",
                }
            ] + [
                {
                    "name": volume.name,
                    "volume_id": "volume-" + volume.id,
                    "is_bootable": volume.is_bootable,
                    "size": volume.size,
                    "pool": self.source_ceph_pool,
                }
                for volume in sources_volumes
            ]
        target_all_volumes_info= [
            {
                "name": volume.name,
                "volume_id": "volume-" + volume.id,
                "is_bootable": volume.is_bootable,
                "size": volume.size,
                "pool": self.target_ceph_pool,
            }
            for volume in target_volumes
        ]
        zip_all_volumes = zip(sources_all_volumes_info, target_all_volumes_info)
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            for source,target in zip_all_volumes:
                future = executor.submit(
                            self.migrate_rbd_data,
                            source["pool"],
                            source["name"],
                            source["volume_id"],
                            target["pool"],
                            target["volume_id"],
                            target["size"],
                            migration_method
                        )
                if future:
                    logging.info(f"[MIGRATION] 从{source['name']},到目标{target['volume_id']}迁移成功")
                
