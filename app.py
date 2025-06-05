import os
import logging
from flask import Flask, request, render_template
from config import UPLOAD_FOLDER, LOG_FILE
from migration_manager import MigrationManager

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# 配置日志记录
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

task_status = {}

@app.route('/')
def index():
    return render_template('index.html')


def run_migration_task(request):
    # 生成任务唯一标识
    excel_file = request.files['excel_file']
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], excel_file.filename)
    excel_file.save(file_path)
    task_id = hash(excel_file.filename + request.form.get('source_auth_url'))
    if task_id in task_status and task_status[task_id] == 'running':
        logging.warning(f"[MIGRATION] 任务 {task_id} 正在运行，避免重复执行。")
        return

    task_status[task_id] = 'running'
    logging.info("[MIGRATION] 开始执行迁移任务")
    source_ceph_conf_file = request.files['source_ceph_conf_file']
    if source_ceph_conf_file:
        source_ceph_conf_path = os.path.join(app.config['UPLOAD_FOLDER'], 'source_ceph.conf')
        source_ceph_conf_file.save(source_ceph_conf_path)

    target_ceph_conf_file = request.files['target_ceph_conf_file']
    if target_ceph_conf_file:
        target_ceph_conf_path = os.path.join(app.config['UPLOAD_FOLDER'], 'target_ceph.conf')
        target_ceph_conf_file.save(target_ceph_conf_path)

    source_auth_args = {
        'auth_url': request.form.get('source_auth_url'),
        'project_name': request.form.get('source_project_name'),
        'username': request.form.get('source_username'),
        'password': request.form.get('source_password'),
        'user_domain_name': request.form.get('source_user_domain_name'),
        'project_domain_name': request.form.get('source_project_domain_name')
    }

    target_auth_args = {
        'auth_url': request.form.get('target_auth_url'),
        'project_name': request.form.get('target_project_name'),
        'username': request.form.get('target_username'),
        'password': request.form.get('target_password'),
        'user_domain_name': request.form.get('target_user_domain_name'),
        'project_domain_name': request.form.get('target_project_domain_name')
    }

    source_ceph_conf = source_ceph_conf_path
    source_ceph_pool = request.form.get('source_ceph_pool')
    target_ceph_conf = target_ceph_conf_path
    target_ceph_pool = request.form.get('target_ceph_pool')
    concurrency = int(request.form.get('concurrency', 1))
    migration_method = request.form.get('migration_method', 'snapshot') 

    try:
        migration_manager = MigrationManager(source_auth_args, target_auth_args, source_ceph_conf, source_ceph_pool, target_ceph_conf, target_ceph_pool)
        migration_manager.batch_migrate_from_excel(file_path,concurrency, migration_method)
        task_status[task_id] = 'completed'
    except Exception as e:
        logging.error(f"[MIGRATION] 执行迁移任务时出现错误: {e}")
        task_status[task_id] = 'error'


@app.route('/migrate', methods=['POST'])
def migrate():
    try:
        run_migration_task(request)
        logging.info("[MIGRATION] 迁移任务已完成")
        return "迁移完成"
    except Exception as e:
        logging.error(f"[MIGRATION] 启动迁移任务时出现错误: {e}")
        return "启动迁移任务时出现错误"


@app.route('/logs')
def get_logs():
    try:
        with open('vm_batch_migration.log', 'r') as f:
            logs = f.read()
        # 过滤出迁移相关日志
        migration_logs = [line for line in logs.split('\n') if '[MIGRATION]' in line]
        return '\n'.join(migration_logs)
    except FileNotFoundError:
        return "日志文件未找到。"


# 新增 /healthz 端点
@app.route('/healthz')
def healthz():
    return "OK", 200


if __name__ == '__main__':
    if not os.path.exists('uploads'):
        os.makedirs('uploads')
    app.run(debug=True, host='0.0.0.0', port=19099)
