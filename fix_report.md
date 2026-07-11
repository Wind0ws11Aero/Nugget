# 异步上下文管理器修复报告

生成时间: 2026-07-11

---

## 扫描结果

扫描了整个代码库（两个副本），共发现 **17 个 bug**，分布在 3 个文件的 2 个代码副本中。

涉及以下类型的错误：
- 对异步上下文管理器使用同步 `with` 语句
- `async def` 函数被调用时缺少 `await`
- `async def` 函数在同步上下文中调用缺少 `asyncio.run()`

---

## 第一轮修复：工作区 /Users/jason/Nugget/ (5 个 bug)

### 修复 #1 — 异步上下文管理器使用同步 `with`

**文件:** `src/restore/bookrestore.py` | **行号:** 180-184

`DvtProvider` 继承自 `DtxServiceProvider`，后者定义了 `__aenter__`/`__aexit__` 但**未**定义 `__enter__`/`__exit__`。

**修改前:**
```python
loop = asyncio.get_running_loop()

async def run_blocking_callback():
    with DvtProvider(rsd) as dvt:
        await apply_bookrestore_files(files, rsd, dvt, ...)

await loop.run_in_executor(None, run_blocking_callback)
```

**修改后:**
```python
async def run_blocking_callback():
    async with DvtProvider(rsd) as dvt:
        await apply_bookrestore_files(files, rsd, dvt, ...)

await run_blocking_callback()
```

### 修复 #2 — 异步函数调用缺少 `await`

**文件:** `src/restore/bookrestore.py` | **行号:** 382

**修改前:** `z_id = generate_bldbmanager(files, temp_dl_manager, afc, server_prefix=server_prefix)`  
**修改后:** `z_id = await generate_bldbmanager(files, temp_dl_manager, afc, server_prefix=server_prefix)`

### 修复 #3 — 异步函数调用缺少 `await`

**文件:** `src/restore/bookrestore.py` | **行号:** 469

**修改前:** `reboot_device(True, lockdown_client=lockdown_client)`  
**修改后:** `await reboot_device(True, lockdown_client=lockdown_client)`

### 修复 #4 — 同步函数中调用 async 函数缺少 `asyncio.run()`

**文件:** `src/restore/restore.py` | **行号:** 382

**修改前:** `perform_restore(backup=back, reboot=reboot, lockdown_client=lockdown_client)`  
**修改后:** `asyncio.run(perform_restore(backup=back, reboot=reboot, lockdown_client=lockdown_client))`

---

## 第二轮修复：运行副本 /Users/jason/NUG/Nugget/ (12 个 bug)

### protective.py (5 处)

| # | 行号 | 修改前 | 修改后 |
|---|------|--------|--------|
| 5 | 388 | `def collect_protective_files(` | `async def collect_protective_files(` |
| 6 | 442 | `with _FastBackupService(lockdown_client) as mb:` | `async with _FastBackupService(lockdown_client) as mb:` |
| 7 | 443 | `is_encrypted = mb.will_encrypt` | `is_encrypted = await mb.get_will_encrypt()` |
| 8 | 453 | `with _FastBackupService(lockdown_client) as mb:` | `async with _FastBackupService(lockdown_client) as mb:` |
| 9 | 454 | `mb.backup(full=True, ...)` | `await mb.backup(full=True, ...)` |

### restore.py (6 处)

| # | 行号 | 修改前 | 修改后 |
|---|------|--------|--------|
| 10 | 153 | `def _restore_ios27(` | `async def _restore_ios27(` |
| 11 | 175 | `collect_protective_files(` | `await collect_protective_files(` |
| 12 | 192 | `perform_restore(backup=back, ...)` | `await perform_restore(backup=back, ...)` |
| 13 | 212 | `lc = create_using_usbmux(serial=udid, ...)` | `lc = await create_using_usbmux(serial=udid, ...)` |
| 14 | 235 | `with Mobilebackup2Service(lc) as mb:` | `async with Mobilebackup2Service(lc) as mb:` |
| 15 | 236 | `mb.restore(` | `await mb.restore(` |
| 16 | 311 | `_restore_ios27(back, ...)` | `await _restore_ios27(back, ...)` |
| 17 | 365 | `perform_restore(backup=back, ...)` | `asyncio.run(perform_restore(backup=back, ...))` |

### bookrestore.py (4 处，工作区修复 #1~#3 的同步)

| # | 行号 | 修改前 | 修改后 |
|---|------|--------|--------|
| — | 178-184 | `with DvtProvider` + `run_in_executor` | `async with DvtProvider` + `await run_blocking_callback()` |
| — | 382 | `z_id = generate_bldbmanager(...)` | `z_id = await generate_bldbmanager(...)` |
| — | 469 | `reboot_device(True, ...)` | `await reboot_device(True, ...)` |

---

## 文件级变更汇总

### 工作区 /Users/jason/Nugget/

| 文件 | 修改次数 |
|------|---------|
| `src/restore/bookrestore.py` | 4 处 |
| `src/restore/restore.py` | 1 处 |

### 运行副本 /Users/jason/NUG/Nugget/

| 文件 | 修改次数 |
|------|---------|
| `src/restore/protective.py` | 5 处 |
| `src/restore/restore.py` | 7 处 |
| `src/restore/bookrestore.py` | 4 处 |

---

## 修复的核心问题

1. **`_FastBackupService`** 继承自 `Mobilebackup2Service` → `LockdownService`，只有 `__aenter__`/`__aexit__`，必须用 `async with`
2. **`DvtProvider`** 继承自 `DtxServiceProvider`，只有 `__aenter__`/`__aexit__`，必须用 `async with`
3. **`Mobilebackup2Service`** 继承自 `LockdownService`，同上
4. 所有 `async def` 函数调用必须加 `await` 或在同步上下文用 `asyncio.run()` 包装
