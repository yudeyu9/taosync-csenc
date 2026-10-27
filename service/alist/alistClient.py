"""
@Author：dr34m
@Date  ：2024/7/4 13:57 
"""
import os
import tempfile
import time
from urllib.parse import quote

import requests

from common.fileFingerprint import fileFingerprint
from common.LNG import G
from service.crypto import csenc


class _ProgressReader:
    """包装可读文件对象，按已读比例回调进度，并支持取消检测。"""

    def __init__(self, fp, total_size, on_ratio=None, should_cancel=None):
        self._fp = fp
        self._total = max(int(total_size or 0), 0)
        self._on_ratio = on_ratio
        self._should_cancel = should_cancel
        self._read = 0

    def read(self, size=-1):
        if self._should_cancel and self._should_cancel():
            raise Exception(G('encrypt_sync_aborted'))
        data = self._fp.read(size)
        if data:
            self._read += len(data)
            if self._on_ratio and self._total > 0:
                self._on_ratio(min(1.0, self._read / float(self._total)))
        return data


def _stage_progress(callback, start, end):
    """将子阶段 0~1 映射到总进度区间 [start, end]（单位：百分比 0~100）。"""
    if callback is None:
        return None

    def report(ratio):
        callback(start + (end - start) * max(0.0, min(1.0, float(ratio))))

    return report


# OpenList/AList 新版任务 state 可能是字符串，统一成 TaoSync 使用的整数状态码。
_TASK_STATE_MAP = {
    '0': 0, 'pending': 0, 'waiting': 0,
    '1': 1, 'running': 1, 'before_retry': 1, 'in_progress': 1,
    '2': 2, 'succeeded': 2, 'success': 2, 'completed': 2,
    '3': 3,
    '4': 4, 'canceled': 4, 'cancelled': 4,
    '5': 5, '6': 6,
    '7': 7, 'failed': 7, 'error': 7, 'errored': 7,
}


def _normalize_task_state(state):
    """将任务 state（int 或 str）规范为 0/1/2/4/7 等整数。"""
    if state is None:
        return 7
    if isinstance(state, bool):
        return 7
    if isinstance(state, int):
        return state
    key = str(state).strip().lower()
    if key in _TASK_STATE_MAP:
        return _TASK_STATE_MAP[key]
    try:
        return int(state)
    except (TypeError, ValueError):
        return 7


def _normalize_task_info(data, task_id=None):
    """统一 copy/upload 任务详情结构，兼容对象或数组、字符串/整数 state。"""
    item = data
    if isinstance(data, list):
        if not data:
            raise Exception('404 task not found')
        item = None
        if task_id is not None:
            for entry in data:
                if isinstance(entry, dict) and str(entry.get('id')) == str(task_id):
                    item = entry
                    break
        if item is None:
            item = data[0]
    if not isinstance(item, dict):
        raise Exception('404 task not found')
    progress = item.get('progress')
    try:
        progress = float(progress) if progress is not None else 0.0
    except (TypeError, ValueError):
        progress = 0.0
    return {
        'id': item.get('id'),
        'name': item.get('name'),
        'state': _normalize_task_state(item.get('state')),
        'status': item.get('status') or '',
        'progress': progress,
        'error': item.get('error') or '',
    }


def _extract_upload_task_id(data):
    """从 /api/fs/put（As-Task）响应 data 中取出任务 id。"""
    if not data:
        return None
    if isinstance(data, dict):
        task = data.get('task') if isinstance(data.get('task'), dict) else data
        task_id = task.get('id') if isinstance(task, dict) else None
        return str(task_id) if task_id else None
    return None


def checkExs(path, rts, spec):
    """
    检查并排除排除项
    :param path: 所在路径
    :param rts: 内容列表，例如
        {
            "test1-1/": {},  # key以/结尾表示目录
            "test1.txt": 4  # 不以/结尾，表示文件，存文件大小
        }
    :param spec: 排除规则
    :return: 排除后的内容列表
    """
    rtsNew = rts.copy()
    base_path = str(path or "").strip("/")
    for rtsItem in rts.keys():
        candidate = "/".join(part for part in (base_path, rtsItem) if part)
        if spec.match_file(candidate):
            del rtsNew[rtsItem]
    return rtsNew


class AlistClient:
    def __init__(self, url, token, alistId=None):
        """
        初始化
        :param url: 请求地址，例如'http://localhost:5244'，注意结尾不要/
        :param alistId: 存在数据库中的id
        :param token: 登录鉴权信息
        """
        self.url = url
        self.user = None
        self.alistId = alistId
        self.token = token
        # key-根目录，val-上次操作时间，或计划下次操作时间，用于间隔等待
        self.waits = {}
        self.getUser()

    def req(self, method, url, data=None, params=None):
        """
        通用请求
        :param method: get/post
        :param url: 请求地址，/api/xxx
        :param data: 需要放在请求体中用json传的数据
        :param params: 在url中的请求参数
        :return: 200返回res['data']，401自动登录后重试，失败抛出异常
        """
        res = {
            'code': 500,
            'message': None,
            'data': None
        }
        headers = None
        if self.token is not None:
            headers = {
                'Authorization': self.token
            }
        try:
            r = requests.request(method, self.url + url, json=data, params=params, headers=headers, timeout=(60, 300))
            if r.status_code == 200:
                res = r.json()
            else:
                res['code'] = r.status_code
                res['message'] = G('code_not_200')
        except Exception as e:
            if 'Invalid URL' in str(e):
                raise Exception(G('address_incorrect'))
            elif 'Max retries' in str(e):
                raise Exception(G('alist_connect_fail'))
            raise Exception(e)
        if res['code'] != 200:
            if res['code'] == 401:
                raise Exception(G('alist_un_auth'))
            raise Exception(G('alist_fail_code_reason').format(res['code'], res['message']))
        return res['data']

    def post(self, url, data=None, params=None):
        """
        发送post请求
        :param url: 请求地址，/api/xxx
        :param data: 需要放在请求体中用json传的数据
        :param params: 放在url中的请求参数
        :return: 200返回res['data']，401自动登录后重试，失败抛出异常
        """
        return self.req('post', url, data, params)

    def get(self, url, params=None):
        """
        发送get请求
        :param url: 请求地址，/api/xxx
        :param params: 放在url中的请求参数
        :return: 200返回res['data']，401自动登录后重试，失败抛出异常
        """
        return self.req('get', url, params=params)

    def getUser(self):
        """
        获取当前用户
        :return:
        """
        self.user = self.get('/api/me')['username']

    def updateAlistId(self, alistId):
        """
        更新alistId
        :param alistId:
        :return:
        """
        self.alistId = alistId

    def checkWait(self, path, scanInterval=0):
        """
        检查是否等待
        :param path: 路径
        :param scanInterval: 间隔
        :return:
        """
        if scanInterval != 0:
            pathFirst = path.split('/', maxsplit=2)[1]
            if pathFirst in self.waits:
                timeC = time.time() - self.waits[pathFirst]
                if timeC < scanInterval:
                    self.waits[pathFirst] = time.time() + timeC
                    time.sleep(scanInterval - timeC)
                    return
            self.waits[pathFirst] = time.time()

    def fileListApi(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        """
        目录列表
        :param path: 目录，以/开头并以/结尾
        :param useCache: 是否使用缓存，0-不使用，1-使用
        :param scanInterval: 目录扫描间隔，单位秒
        :param spec: 排除项规则
        :return: {
            "test1-1/": {},  # key以/结尾表示目录
            "test1.txt": 4  # 不以/结尾，表示文件，存文件大小
        }
        :param rootPath: 同步根目录
        """
        details = self.fileListDetailApi(
            path, useCache, scanInterval, spec, rootPath
        )
        return {
            name: {} if detail['isDir'] else detail['size']
            for name, detail in details.items()
        }

    def fileListDetailApi(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        """Return AList entries with stable metadata while keeping fileListApi compatible."""
        self.checkWait(path, scanInterval)
        res = self.post('/api/fs/list', data={
            'path': path,
            'refresh': useCache != 1
        })['content']
        if res is not None:
            rts = {
                f"{item['name']}/" if item['is_dir'] else item['name']: {
                    'isDir': 1 if item['is_dir'] else 0,
                    'size': None if item['is_dir'] else item['size'],
                    'fingerprint': fileFingerprint(
                        'alist',
                        item.get('hash_info') or item.get('hash'),
                        item.get('modified') or item.get('updated_at'),
                    ),
                } for item in res
            }
        else:
            rts = {}
        if spec and rts:
            if rootPath is None:
                rootPath = path
            rts = checkExs(path[len(rootPath):], rts, spec)
        return rts

    def filePathList(self, path):
        """
        通过路径获取其下路径列表
        :param path:
        :return:
        """
        res = self.post('/api/fs/list', data={
            'path': path,
            'refresh': True
        })['content']
        if res is not None:
            return [{'path': item['name']} for item in res if item['is_dir']]
        else:
            return []

    def allFileList(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        """
        递归获取文件列表，暂时弃用
        :param path: 根路径
        :param useCache: 是否使用缓存，0-不使用，1-使用
        :param scanInterval: 目录扫描间隔，单位秒
        :param spec: 排除项规则
        :param rootPath: 同步根目录
        :return: {
            "test1-1/": {
                "test1-3/": {
                    "test1.txt": 4
                },
                "test1.txt": 4
            },
            "test1.txt": 4
        }
        """
        if rootPath is None:
            rootPath = path
        fList = self.fileListApi(path, useCache, scanInterval, spec, rootPath)
        for key in fList.keys():
            if key.endswith('/'):
                fList[key] = self.allFileList(f"{path}/{key[:-1]}", useCache, scanInterval, spec, rootPath)
        return fList

    def mkdir(self, path, scanInterval=0):
        """
        创建目录
        :param path: 路径
        :param scanInterval:
        """
        self.checkWait(path, scanInterval)
        return self.post('/api/fs/mkdir', data={
            'path': path
        })

    def deleteFile(self, path, names, scanInterval=0):
        """
        删除文件或目录
        :param path: 路径
        :param names: 文件/目录名，列表
        :param scanInterval:
        """
        self.checkWait(path, scanInterval)
        self.post('/api/fs/remove', data={
            'names': names,
            'dir': path
        })

    @staticmethod
    def _joinFilePath(directory, name):
        """拼接目录与文件名为 OpenList 绝对路径。"""
        base = (directory or '/').rstrip('/')
        if not base:
            return '/' + str(name)
        return base + '/' + str(name)

    def getFileInfo(self, path):
        """获取单个文件/目录信息（含 raw_url）。"""
        return self.post('/api/fs/get', data={'path': path})

    def downloadToFile(self, path, target_fp, progress_callback=None, should_cancel=None):
        """通过 raw_url 将远端文件流式下载到本地文件对象。

        :param progress_callback: 可选，接收 0.0~1.0 下载比例
        :param should_cancel: 可选，返回 True 时中止下载
        """
        info = self.getFileInfo(path)
        raw_url = info.get('raw_url') if info else None
        if not raw_url:
            raise Exception(G('alist_download_url_missing').format(path))
        if raw_url.startswith('/'):
            raw_url = self.url + raw_url
        headers = {}
        if self.token is not None:
            headers['Authorization'] = self.token
        try:
            with requests.get(
                raw_url, headers=headers, stream=True, timeout=(60, 600)
            ) as response:
                if response.status_code != 200:
                    raise Exception(
                        G('alist_fail_code_reason').format(
                            response.status_code, response.text[:200]
                        )
                    )
                total = 0
                try:
                    total = int(response.headers.get('Content-Length') or 0)
                except (TypeError, ValueError):
                    total = 0
                if total <= 0:
                    try:
                        total = int((info or {}).get('size') or 0)
                    except (TypeError, ValueError):
                        total = 0
                done = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if should_cancel and should_cancel():
                        raise Exception(G('encrypt_sync_aborted'))
                    if chunk:
                        target_fp.write(chunk)
                        done += len(chunk)
                        if progress_callback and total > 0:
                            progress_callback(min(1.0, done / float(total)))
                if progress_callback:
                    progress_callback(1.0)
        except Exception as e:
            if isinstance(e, requests.RequestException):
                raise Exception(G('alist_connect_fail')) from e
            raise

    def uploadFromFile(self, path, source_fp, size, progress_callback=None, should_cancel=None):
        """通过 /api/fs/put 以异步任务方式上传本地文件流到 OpenList。

        As-Task=true：HTTP 只等待「文件交到 OpenList」，网盘侧传输在后台任务完成，
        避免夸克等慢速驱动把 put 堵到超时（与 Cloud Sync + WebDAV 同类问题）。

        :param progress_callback: 可选，接收 0.0~1.0（TaoSync→OpenList 收文件比例）
        :param should_cancel: 可选，返回 True 时中止上传
        :return: OpenList upload 任务 id；极端情况下无 task 则返回 None
        """
        headers = {
            'Authorization': self.token,
            'File-Path': quote(path, safe=''),
            'Content-Type': 'application/octet-stream',
            'Content-Length': str(int(size)),
            'As-Task': 'true',
            'Overwrite': 'true',
        }
        body = source_fp
        if progress_callback is not None or should_cancel is not None:
            body = _ProgressReader(source_fp, size, progress_callback, should_cancel)
        try:
            # 读超时只覆盖本机/局域网把密文交给 OpenList，不再覆盖 OpenList→网盘。
            # 大文件局域网传输仍可能较久，故放宽到 1 小时。
            response = requests.put(
                self.url + '/api/fs/put',
                data=body,
                headers=headers,
                timeout=(60, 3600),
            )
        except Exception as e:
            if isinstance(e, requests.RequestException):
                raise Exception(G('alist_connect_fail')) from e
            raise
        if response.status_code != 200:
            raise Exception(
                G('alist_fail_code_reason').format(
                    response.status_code, response.text[:200]
                )
            )
        try:
            payload = response.json()
        except ValueError as e:
            raise Exception(G('alist_fail_code_reason').format(500, response.text[:200])) from e
        if payload.get('code') != 200:
            raise Exception(
                G('alist_fail_code_reason').format(
                    payload.get('code'), payload.get('message')
                )
            )
        if progress_callback:
            progress_callback(1.0)
        return _extract_upload_task_id(payload.get('data'))

    def _copyFileEncrypted(self, srcDir, dstDir, name, encrypt_password, encrypt_public_key,
                           progress_callback=None, should_cancel=None):
        """下载明文 → 本机加密 → 异步上传密文（不走远端 fs/copy）。

        本机阶段进度：下载 0~40%、加密 40~70%、交给 OpenList 70~85%。
        返回 upload 任务 id，由调用方轮询 OpenList→网盘进度（建议映射到 85~100%）。
        若未返回任务 id（旧版/异常），则视为 put 已同步完成。
        """
        src_path = self._joinFilePath(srcDir, name)
        dst_path = self._joinFilePath(dstDir, name)
        plain_path = None
        enc_path = None
        try:
            if progress_callback:
                progress_callback(0.0)
            with tempfile.NamedTemporaryFile(prefix='taosync-alist-plain-', delete=False) as plain_fp:
                plain_path = plain_fp.name
                self.downloadToFile(
                    src_path,
                    plain_fp,
                    progress_callback=_stage_progress(progress_callback, 0.0, 40.0),
                    should_cancel=should_cancel,
                )
            if should_cancel and should_cancel():
                raise Exception(G('encrypt_sync_aborted'))
            with tempfile.NamedTemporaryFile(prefix='taosync-alist-enc-', delete=False) as enc_fp:
                enc_path = enc_fp.name
            plain_size = os.path.getsize(plain_path)
            with open(plain_path, 'rb') as plain_fp, open(enc_path, 'wb') as enc_fp:
                reader = _ProgressReader(
                    plain_fp,
                    plain_size,
                    _stage_progress(progress_callback, 40.0, 70.0),
                    should_cancel,
                )
                csenc.encrypt_stream(
                    reader,
                    enc_fp,
                    encrypt_password,
                    file_name=name,
                    public_key=encrypt_public_key,
                )
            if should_cancel and should_cancel():
                raise Exception(G('encrypt_sync_aborted'))
            enc_size = os.path.getsize(enc_path)
            with open(enc_path, 'rb') as enc_fp:
                task_id = self.uploadFromFile(
                    dst_path,
                    enc_fp,
                    enc_size,
                    progress_callback=_stage_progress(progress_callback, 70.0, 85.0),
                    should_cancel=should_cancel,
                )
            if task_id:
                if progress_callback:
                    progress_callback(85.0)
                return task_id
            if progress_callback:
                progress_callback(100.0)
            return None
        finally:
            for path in (plain_path, enc_path):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def copyFile(self, srcDir, dstDir, name, encrypt_password=None, encrypt_public_key=None,
                 progress_callback=None, should_cancel=None):
        """
        复制文件
        :param srcDir: 源目录
        :param dstDir: 目标目录
        :param name: 文件名
        :param encrypt_password: 非空时本机加密后再异步上传，不走远端 fs/copy
        :param encrypt_public_key: RSA 公钥 PEM，与密码一起写入双通道元数据
        :param progress_callback: 加密同步时可选，接收 0~100 本机阶段进度
        :param should_cancel: 加密同步时可选，返回 True 则中止
        :return: 任务id（加密为 upload 任务，否则为 copy 任务）；无任务时返回 None
        """
        if encrypt_password:
            return self._copyFileEncrypted(
                srcDir, dstDir, name, encrypt_password, encrypt_public_key,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
        tasks = self.post('/api/fs/copy', data={
            'src_dir': srcDir,
            'dst_dir': dstDir,
            'overwrite': True,
            'names': [
                name
            ]
        })['tasks']
        if tasks:
            return tasks[0]['id']
        else:
            return None

    def moveFile(self, srcDir, dstDir, name):
        """
        移动文件
        :param srcDir: 源目录
        :param dstDir: 目标目录
        :param name: 文件名
        :return: 任务id
        """
        tasks = self.post('/api/fs/move', data={
            'src_dir': srcDir,
            'dst_dir': dstDir,
            'overwrite': True,
            'names': [
                name
            ]
        })['tasks']
        if tasks:
            return tasks[0]['id']
        else:
            return None

    def _taskApiPaths(self, taskType, action):
        """生成任务 API 候选路径（兼容 admin 旧路径与 OpenList 新路径）。"""
        kind = 'upload' if str(taskType).lower() == 'upload' else 'copy'
        return [
            '/api/admin/task/{}/{}'.format(kind, action),
            '/api/task/{}/{}'.format(kind, action),
        ]

    def _taskPost(self, taskType, action, taskId=None):
        """按候选路径依次尝试任务 POST，成功即返回 data。

        仅在路径疑似不存在时回退到备用 URL，避免吞掉真实业务错误。
        """
        params = {'tid': taskId} if taskId is not None else None
        paths = self._taskApiPaths(taskType, action)
        last_error = None
        for index, url in enumerate(paths):
            try:
                return self.post(url, params=params)
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                can_fallback = index + 1 < len(paths) and any(
                    token in msg for token in ('404', 'not found', 'not_found', '405', 'not implement')
                )
                if can_fallback:
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise Exception(G('alist_connect_fail'))

    def taskInfo(self, taskId, taskType='copy'):
        """
        任务详情（copy 或 upload）
        :param taskId: 任务id
        :param taskType: 'copy' | 'upload'
        :return: {
            "id": "...",
            "name": "...",
            "state": 2,  # 0等待 1进行中 2成功 4取消 7失败（已规范化）
            "status": "...",
            "progress": 0,
            "error": ""
        }
        """
        data = self._taskPost(taskType, 'info', taskId)
        return _normalize_task_info(data, taskId)

    def copyTaskDone(self):
        """
        已完成的复制任务
        :return: [{
            "id": "26GQSD1mZHDlDq1V1Lf7G",
            "name": "copy [/test1](/test1/test1.txt) to [/test2](/)",
            "state": 2, # 0-等待中，1-进行中，2-成功
            "status": "getting src object",
            "progress": 0, # 进度
            "error": ""
        }]
        """
        return self.get('/api/admin/task/copy/done')

    def copyTaskUnDone(self):
        """
        未完成的复制任务
        :return: [{
            "id": "26GQSD1mZHDlDq1V1Lf7G",
            "name": "copy [/test1](/test1/test1.txt) to [/test2](/)",
            "state": 1, # 0-等待中，1-进行中，2-成功
            "status": "getting src object",
            "progress": 0, # 进度
            "error": ""
        }]
        """
        return self.get('/api/admin/task/copy/undone')

    def copyTaskRetry(self, taskId):
        """
        重试复制任务
        :param taskId: 任务id
        """
        self.post('/api/admin/task/copy/retry', params={
            'tid': taskId
        })

    def copyTaskClearSucceeded(self):
        """
        清除已成功的复制任务
        """
        self.post('/api/admin/task/copy/clear_succeeded')

    def copyTaskDelete(self, taskId):
        """
        删除复制任务
        :param taskId: 任务id
        """
        self.taskDelete(taskId, 'copy')

    def copyTaskCancel(self, taskId):
        """
        取消复制任务
        :param taskId: 任务id
        """
        self.taskCancel(taskId, 'copy')

    def taskDelete(self, taskId, taskType='copy'):
        """删除 copy/upload 任务。"""
        self._taskPost(taskType, 'delete', taskId)

    def taskCancel(self, taskId, taskType='copy'):
        """取消 copy/upload 任务。"""
        self._taskPost(taskType, 'cancel', taskId)
