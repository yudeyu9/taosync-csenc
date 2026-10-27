"""群晖 Cloud Sync 兼容的文件加密（v3.1，密码 + RSA 双通道）。

写出的密文格式与群晖 Cloud Sync 一致：
- enc_key1 / key1_hash：密码通道
- enc_key2 / key2_hash：RSA 公钥通道（官方解密工具需要）
可用官方或开源解密工具按密码或 private.pem 还原明文。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import random
import string
import struct
from collections import OrderedDict

import lz4.frame
from Cryptodome.Cipher import AES, PKCS1_OAEP
from Cryptodome.IO import PEM
from Cryptodome.PublicKey import RSA
from Cryptodome.Util.asn1 import DerSequence
from Cryptodome.Util.Padding import pad, unpad

# ====== 常量定义 ======
MAGIC = b"__CLOUDSYNC_ENC__"  # 文件头魔数
MAGIC_HASH = hashlib.md5(MAGIC).hexdigest().encode("ascii")  # 用于额外校验的魔数字符串的 MD5

_DATA_CHUNK_TARGET = 8192  # 分块大小（官方多见约 8KB 一块，TLV 中 bytes 字段上限 65535）
_LZ4_INPUT_BLOCK = 4096    # lz4 每块未压缩最大 4KB（-B4096），配合 auto_flush 与官方一致

_HASH_SALT_CHARS = string.ascii_letters + string.digits  # hash salt 可用字符集


def _openssl_kdf(password, salt, key_size=32, iv_size=16):
    """
    OpenSSL EVP_BytesToKey（MD5），与群晖 Cloud Sync 派生 key/iv 方式一致。
    兼容 openssl enc 的算法。output: (key, iv)
    """
    if isinstance(password, str):
        password = password.encode("utf-8")
    if isinstance(salt, str):
        salt = salt.encode("ascii")
    # 如果没有 salt，则只做 1 轮加密，v3 起有 salt 时做 1000 轮（安全性高一些）
    count = 1 if salt == b"" else 1000
    derived = b""
    block = b""
    while len(derived) < key_size + iv_size:
        hasher_input = block + password + salt
        block = hasher_input
        for _ in range(count):
            block = hashlib.md5(block).digest()
        derived += block
    # 返回 key, iv，两者拼接后的结果
    return derived[:key_size], derived[key_size:key_size + iv_size]


def _aes_cbc(password, salt):
    """
    由 password+salt 派生 AES-256-CBC 加解密器。
    key/iv 来源同 _openssl_kdf。
    """
    key, iv = _openssl_kdf(password, salt)
    return AES.new(key, AES.MODE_CBC, iv)


def salted_hash_of(salt10, data):
    """
    生成 Cloud Sync 元数据用的 salted hash：salt10 + md5(salt10||data).hex。
    通常用于验证 key1、session key 等。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    digest = hashlib.md5(salt10.encode("ascii") + data).hexdigest()
    return salt10 + digest


def _random_salt(length):
    """
    生成指定长度的随机字母数字 salt。
    用于加密字段的随机盐或 hash salt。
    """
    return "".join(random.choice(_HASH_SALT_CHARS) for _ in range(length))


def _normalize_pem(pem):
    """
    规范化 PEM，为 LF 换行并保证末尾只有一个换行（官方工具一致）。
    """
    if pem is None:
        return None
    if isinstance(pem, bytes):
        pem = pem.decode("utf-8")
    pem = pem.strip().replace("\r\n", "\n") + "\n"
    return pem


def _export_pkcs1_public_pem(key):
    """
    导出群晖 key.zip 同 PKCS#1 公钥 PEM（"BEGIN RSA PUBLIC KEY"）
    必须用 PKCS#1 格式用于官方 key2_hash 校验，否则官方工具无法解密。
    """
    # RSAPublicKey ::= SEQUENCE { modulus INTEGER, publicExponent INTEGER }
    der = DerSequence([int(key.n), int(key.e)]).encode()
    return _normalize_pem(PEM.encode(der, "RSA PUBLIC KEY"))


def generate_rsa_keypair():
    """
    生成 2048 位 RSA 密钥对，并以群晖格式导出 (pkcs1 公钥 PEM, 私钥 PEM)。
    """
    key = RSA.generate(2048)
    public_pem = _export_pkcs1_public_pem(key)
    private_pem = _normalize_pem(key.export_key(format="PEM", pkcs=1).decode("ascii"))
    return public_pem, private_pem


def _to_synology_public_pem(public_pem):
    """
    将任意格式公钥标准化为群晖（PKCS#1 PEM，"BEGIN RSA PUBLIC KEY"）。
    确保 key2_hash 与官方算法对齐。
    """
    public_pem = _normalize_pem(public_pem)
    if not public_pem:
        return None
    return _export_pkcs1_public_pem(RSA.import_key(public_pem))


def _encrypt_session_key_with_public(session_key_hex, public_pem):
    """
    使用 RSA-OAEP 公钥封装会话密钥，返回 (enc_key2_b64, key2_hash)。
    key2_hash 必须用 PKCS#1 PEM 计算，否则官方工具无法识别。
    """
    public_pem = _to_synology_public_pem(public_pem)
    if not public_pem:
        raise ValueError("public_key is required")
    cipher = PKCS1_OAEP.new(RSA.import_key(public_pem))
    encrypted = cipher.encrypt(session_key_hex)
    enc_key2 = base64.b64encode(encrypted).decode("ascii")
    key2_hash = salted_hash_of(_random_salt(10), public_pem)
    return enc_key2, key2_hash


# ========== TLV 编解码工具函数 ==========

def _write_int(out, value):
    """
    写出 TLV 整数（lead=0x01）。
    """
    if value < 0:
        raise ValueError("negative integers are not supported")
    if value == 0:
        out.write(b"\x01\x01\x00")
        return
    hex_digits = format(value, "x")
    if len(hex_digits) % 2:
        hex_digits = "0" + hex_digits
    raw = binascii.unhexlify(hex_digits)
    out.write(b"\x01")  # 类型标识
    out.write(struct.pack(">B", len(raw)))  # 长度（1字节，实际不会很大）
    out.write(raw)


def _write_bytes(out, value):
    """
    写出 TLV bytes（lead=0x11，长度 uint16，最大 65535）。
    """
    if len(value) > 0xFFFF:
        raise ValueError("bytes field too large for Cloud Sync TLV")
    out.write(b"\x11")
    out.write(struct.pack(">H", len(value)))
    out.write(value)


def _write_string(out, value):
    """
    写出 TLV 字符串（lead=0x10）。
    """
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("string field too large for Cloud Sync TLV")
    out.write(b"\x10")
    out.write(struct.pack(">H", len(encoded)))
    out.write(encoded)


def _write_dict(out, mapping):
    """
    写出 TLV 字典（lead=0x42），字段及嵌套结构采用递归，结尾 0x40 表示字典结束。
    """
    out.write(b"\x42")
    for key, value in mapping.items():
        _write_string(out, key)
        if isinstance(value, dict):
            _write_dict(out, value)
        elif isinstance(value, int) and not isinstance(value, bool):
            _write_int(out, value)
        elif isinstance(value, (bytes, bytearray)):
            _write_bytes(out, bytes(value))
        elif isinstance(value, str):
            _write_string(out, value)
        else:
            raise TypeError("unsupported Cloud Sync metadata type: %r" % (type(value),))
    out.write(b"\x40")  # 字典尾


def _read_object(stream):
    """
    从流读取一个 TLV 对象。遇 EOF/0x40 返回 None。
    支持字典嵌套递归解析及 bytes/int/string。
    """
    header = stream.read(1)
    if not header:
        return None
    kind = header[0]
    if kind == 0x42:  # 新子字典
        result = OrderedDict()
        while True:
            key = _read_object(stream)
            if key is None:
                break
            result[key] = _read_object(stream)
        return result
    if kind == 0x40:  # 字典结尾
        return None
    if kind == 0x11:
        length = struct.unpack(">H", stream.read(2))[0]
        return stream.read(length)
    if kind == 0x10:
        length = struct.unpack(">H", stream.read(2))[0]
        return stream.read(length).decode("utf-8")
    if kind == 0x01:
        length = struct.unpack(">B", stream.read(1))[0]
        raw = stream.read(length)
        return int(binascii.hexlify(raw), 16) if raw else 0
    raise ValueError("unknown Cloud Sync type byte 0x%02X" % kind)


def _decode_stream(stream):
    """
    解析 Cloud Sync 文件魔数与内容，并依次 yield 解出来的字典对象。
    校验魔数 & 魔数 md5，一致才继续解析。
    """
    magic = stream.read(len(MAGIC))
    if magic != MAGIC:
        raise ValueError("not a Synology Cloud Sync encrypted file")
    magic_hash = stream.read(32)
    if magic_hash != MAGIC_HASH:
        raise ValueError("invalid Cloud Sync magic hash")
    while True:
        obj = _read_object(stream)
        if obj is None:
            break
        if not isinstance(obj, dict):
            raise ValueError("expected dictionary object in Cloud Sync stream")
        yield obj


# ========== 加密主流程函数 ==========

def encrypt_stream(instream, outstream, password, file_name="file", public_key=None):
    """
    将明文流加密为群晖 Cloud Sync v3.1 格式。

    :param password: 加密密码（必填，写 enc_key1）
    :param public_key: RSA 公钥 PEM（必填以兼容官方工具，写 enc_key2）
    :param file_name: 写入元数据的原始文件名
    """
    if not password:
        raise ValueError("password is required")
    if isinstance(password, bytes):
        password = password.decode("utf-8")
    public_key = _normalize_pem(public_key)
    if not public_key:
        raise ValueError("public_key is required for official Cloud Sync compatibility")

    # ===== 1. 生成 session key 及加密头部字段 =====
    session_key_hex = binascii.hexlify(os.urandom(32))           # 32 字节的 AES 会话密钥 hex 编码
    salt = _random_salt(8)                                       # 8 字节 salt
    key1_hash = salted_hash_of(_random_salt(10), password)       # 用于密码校验
    session_key_hash = salted_hash_of(_random_salt(10), session_key_hex)  # 用于校验解密后的 session key

    # 加密 session key（对称部分，enc_key1）: password+salt 衍生密钥 XOR 明文 session key 后 b64
    enc_key1 = base64.b64encode(
        _aes_cbc(password, salt.encode("ascii")).encrypt(pad(session_key_hex, AES.block_size))
    ).decode("ascii")

    # 加密 session key（非对称部分，enc_key2）: RSA-OAEP 封装
    enc_key2, key2_hash = _encrypt_session_key_with_public(session_key_hex, public_key)

    # ===== 2. 写文件头及 metadata 字典 =====
    outstream.write(MAGIC)
    outstream.write(MAGIC_HASH)

    # TLV 字典字段顺序与官方样例保持一致，避免兼容性问题
    _write_dict(outstream, OrderedDict([
        ("compress", 1),
        ("digest", "md5"),
        ("enc_key1", enc_key1),
        ("enc_key2", enc_key2),
        ("encrypt", 1),
        ("file_name", str(file_name)),
        ("key1_hash", key1_hash),
        ("key2_hash", key2_hash),
        ("salt", salt),
        ("session_key_hash", session_key_hash),
        ("type", "metadata"),
        ("version", OrderedDict([("major", 3), ("minor", 1)])),
    ]))

    # ===== 3. 初始化流式压缩和对称 AES 加密器 =====
    content_cipher = _aes_cbc(binascii.unhexlify(session_key_hex), b"")  # 用 session key 派生真正内容密钥，AES-256-CBC，无 salt
    compressor = lz4.frame.LZ4FrameCompressor(
        block_size=lz4.frame.BLOCKSIZE_MAX64KB,  # lz4 帧最大 64KB（实际用 4KB 刷块）
        block_linked=True,        # -BD，块间相关，兼容官方
        content_checksum=False,
        block_checksum=False,
        auto_flush=True,          # 保证每 4KB 一刷出压缩块
    )
    md5_digest = hashlib.md5()    # 累积明文 MD5
    pending = bytearray(compressor.begin())  # 存储待加密的已压缩数据
    plain_buf = bytearray()       # 临时保存未送入压缩的数据

    def emit_encrypted_chunk(raw):
        """
        将压缩对齐的数据加密后，写出 type=data 字典分块。
        供 flush 调用。
        """
        encrypted = content_cipher.encrypt(raw)
        _write_dict(outstream, OrderedDict([
            ("data", encrypted),
            ("type", "data"),
        ]))

    def flush_full_blocks():
        """
        响应 pending buffer 完成一个 8KB 块或更多时，分块输出。
        每块保证是 AES block size 的整数倍，便于解密时对齐。
        """
        nonlocal pending
        while len(pending) >= _DATA_CHUNK_TARGET:
            usable = _DATA_CHUNK_TARGET - (_DATA_CHUNK_TARGET % AES.block_size)  # 对齐 block
            chunk = bytes(pending[:usable])
            del pending[:usable]
            emit_encrypted_chunk(chunk)

    def compress_plain(data):
        """
        明文数据按 4KB 分批送入 LZ4，使压缩块分布与官方生成一致。
        每次处理后尝试 flush（加密并输出），保证流式高效。
        """
        nonlocal plain_buf
        plain_buf.extend(data)
        while len(plain_buf) >= _LZ4_INPUT_BLOCK:
            piece = bytes(plain_buf[:_LZ4_INPUT_BLOCK])
            del plain_buf[:_LZ4_INPUT_BLOCK]
            pending.extend(compressor.compress(piece))
            flush_full_blocks()

    # ===== 4. 流式读取，每次较大块读取加速处理 =====
    while True:
        plain = instream.read(1024 * 1024)
        if not plain:
            break
        md5_digest.update(plain)  # 用于后续完整性校验
        compress_plain(plain)

    # ===== 5. 清空缓冲区，flush 收尾 =====
    if plain_buf:
        pending.extend(compressor.compress(bytes(plain_buf)))
        plain_buf.clear()
        flush_full_blocks()
    pending.extend(compressor.flush())
    flush_full_blocks()
    # 末尾最后一块需补齐到 AES.block_size
    emit_encrypted_chunk(pad(bytes(pending), AES.block_size))
    # 写 MD5 摘要元数据，供解密端校验
    _write_dict(outstream, OrderedDict([
        ("file_md5", md5_digest.hexdigest()),
        ("type", "metadata"),
    ]))


def decrypt_stream(instream, outstream, password=None, private_key=None):
    """
    解密 Cloud Sync v3.1。解密时可用密码或 RSA 私钥。
    自动解析分块与解压，支持校验密码/session key。
    """
    # -------- 输入参数检查及初始化变量 --------
    if not password and not private_key:
        raise ValueError("password or private_key is required")
    if isinstance(password, bytes):
        password = password.decode("utf-8")
    private_key = _normalize_pem(private_key)

    enc_key1_bytes = None      # 密码通道，base64
    enc_key2_bytes = None      # 公钥通道，base64
    salt = b""                # 加密时用到的 salt
    session_key_hash = None    # 校验 session key 完整性
    expected_md5 = None        # 最终明文 md5
    md5_digest = hashlib.md5() # 解密逐块累加明文 md5 用
    decryptor = None           # 内容解密器
    decrypted_chunk = None     # 临时存放解密未解包的压缩块
    decompressor = lz4.frame.LZ4FrameDecompressor()  # lz4 解压器

    def emit_compressed(chunk):
        """
        将一个解密完的压缩块解压、写出明文，并累计 md5_digest。
        """
        if not chunk:
            return
        plain = decompressor.decompress(chunk)
        if plain:
            outstream.write(plain)
            md5_digest.update(plain)

    # -------- 主循环，逐对象处理每个 TLV 字典分块 --------
    for obj in _decode_stream(instream):
        obj_type = obj.get("type")
        if obj_type == "metadata":
            # 首个 metadata 记录关键信息，提取字段
            if "enc_key1" in obj:
                enc_key1_bytes = base64.b64decode(obj["enc_key1"].encode("ascii"))
            if "enc_key2" in obj:
                enc_key2_bytes = base64.b64decode(obj["enc_key2"].encode("ascii"))
            if "salt" in obj:
                salt = obj["salt"].encode("ascii")
            if "session_key_hash" in obj:
                session_key_hash = obj["session_key_hash"]
            # 用 hash 验证密码正确性（防止瞎试）
            if "key1_hash" in obj and password is not None:
                expected = obj["key1_hash"]
                actual = salted_hash_of(expected[:10], password)
                if actual != expected:
                    raise ValueError("incorrect Cloud Sync encryption password")
            if "file_md5" in obj:
                expected_md5 = obj["file_md5"]
            continue  # metadata 块，不用解密内容

        if obj_type != "data":
            continue  # 忽略非 data（和 metadata 外的稀有情况）

        if decryptor is None:
            # 第一次遇到 data 块时，先根据密码或私钥解开 session key
            session_key = None
            if password is not None and enc_key1_bytes is not None:
                # 密码通道：session key 是 enc_key1 (b64 解码后解密+unpad)
                session_key = unpad(
                    _aes_cbc(password, salt).decrypt(enc_key1_bytes),
                    AES.block_size,
                )
            elif private_key is not None and enc_key2_bytes is not None:
                # 私钥通道：enc_key2 (b64 解码后用 RSA 私钥解包)
                session_key = PKCS1_OAEP.new(RSA.import_key(private_key)).decrypt(enc_key2_bytes)
            if session_key is None:
                raise ValueError(
                    "not enough information to decrypt: need password+enc_key1 or private_key+enc_key2"
                )
            # 校验 session key 是否被篡改（比对 hash）
            if session_key_hash is not None:
                actual_hash = salted_hash_of(session_key_hash[:10], session_key)
                if actual_hash != session_key_hash:
                    raise ValueError("session key hash mismatch")
            # 注意解密用的 material，带 salt 时是 hex 解包出来的二进制
            material = binascii.unhexlify(session_key) if salt else session_key
            decryptor = _aes_cbc(material, b"")

        # 上一 data 块已解出的内容需要先输出（数据有缓冲一拍）
        if decrypted_chunk:
            emit_compressed(decrypted_chunk)
        decrypted_chunk = decryptor.decrypt(obj["data"])

    # 循环结束后，还有数据需要输出，同时解包 lz4 block 补齐
    if decrypted_chunk:
        emit_compressed(unpad(decrypted_chunk, AES.block_size))

    # 校验明文 md5 是否一致（防篡改检测）
    if expected_md5 is not None and md5_digest.hexdigest() != expected_md5:
        raise ValueError(
            "plaintext md5 mismatch: expected %s got %s"
            % (expected_md5, md5_digest.hexdigest())
        )


# ========== 文件路径级加解密（直接操作磁盘文件） ==========

def encrypt_file(src_path, dst_path, password, file_name=None, public_key=None):
    """
    将磁盘上的明文文件加密为 Cloud Sync 格式，自动处理流。
    :param file_name: 指定元数据里的原始文件名。默认为源文件名
    """
    name = file_name if file_name is not None else os.path.basename(src_path)
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        encrypt_stream(src, dst, password, file_name=name, public_key=public_key)


def decrypt_file(src_path, dst_path, password=None, private_key=None):
    """
    将磁盘上的 Cloud Sync 密文文件解密为明文。
    """
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        decrypt_stream(src, dst, password=password, private_key=private_key)
