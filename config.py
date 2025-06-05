import os

# 配置信息
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

default_vm_pass = "P@ssw0rd"
DEFAULT_MIGRATE_IMAGE="迁移通用镜像QCOW2"
DEFAULT_CINDER_TYPE="hdd"
DEFAULT_SEC_GROUP="all_pass"


# 日志配置
LOG_FILE = 'vm_batch_migration.log'
