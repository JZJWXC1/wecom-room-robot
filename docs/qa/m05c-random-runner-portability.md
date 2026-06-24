# M0.5C 随机 fallback QA 写入可移植性修复报告

## 范围

本轮只修复随机 fallback QA 在 Windows 下 artifact 路径 / 写入中断的可移植性问题。

没有修改：

- `app/main.py`
- `app/config.py`
- `app/services/*`
- 知识卡片
- 客服回复逻辑
- 工具执行和发送逻辑

## 复现与异常定位

M0.5B 记录到的异常：

- 异常类型：`OSError`
- errno：`22`
- 发生位置：`qa_artifacts/run_rag_10windows_10turns_utf8.py` 的 `artifact.write_text(...)`
- 阶段：最终 artifact open/write，而不是目录创建、文件名生成、JSON 序列化或 rename/replace
- 目标文件名：`rag_random_guard_utf8_20260624_200910.json`
- 文件名来源：固定 ASCII prefix + 无冒号时间戳，不包含用户原话、查询文本或模型回复
- 文件名非法字符：未发现

M0.5C 用相同 seed `20260624`、隔离 cwd、清空敏感环境后重跑，没有再次触发同一异常；随机 QA 正常完整执行，并按业务质量失败返回 exit code 3。

因此本轮按“写入层不够稳健”的可移植性问题处理：原实现直接 `Path.write_text` 覆盖最终 JSON，缺少临时文件 + 原子替换；写入失败时也没有明确的基础设施失败 artifact。

## 修改内容

链路归属：QA runner / 测试基础设施，不属于客服业务链路。

- 新增稳定 ASCII artifact 文件名生成：`artifact_path_for(...)`
- artifact 文件名不再可能使用用户原话、查询文本或模型回复
- 写 JSON 改为临时文件 + `Path.replace(...)` 原子替换
- artifact 内路径改为仓库相对 POSIX 路径
- 新增 `canonical_result_hash`，排除时间戳和耗时字段，但保留回复、状态、严重等级和证据
- 写入失败时生成 `completed=false / infrastructure_error=true / exit_code=2` 的失败 payload
- 随机 QA runner 捕获 `ArtifactWriteError` 并以 2 退出

## 新增测试

- 路径包含中文和空格时可以完整写入 JSON
- 用户问题包含冒号、问号、斜杠、反斜杠、引号、换行时，不进入 artifact 文件名
- Windows 风格路径在 artifact 内使用仓库相对 POSIX 路径
- 写入失败 payload 不会产生 `completed=true`
- 临时 artifact 原子切换成功
- canonical hash 对动态时间 / 耗时稳定，对回复内容变化敏感
- M0.5B 新增图片 fixture 是非空 PNG，签名为 `89 50 4E 47 0D 0A 1A 0A`，写在系统临时目录并可清理

## 验证结果

- 新增 / 相关测试：`6 passed, 2 warnings`
- `python -m compileall -q app scripts tests`：exit 0
- `python -m pytest -q tests`：`518 passed, 1 deselected, 2 warnings, 2 subtests passed`
- 固定离线 QA 同 seed 两次：两次均 `QUALITY passed=True infrastructure_error=False exit_code=0`
- 随机 fallback QA 同 seed 两次：
  - run1：`completed=true`，`actual_windows=10`，`actual_turns=100`，`passed=false`，`exit_code=3`，`high=6`，`medium=2`，`fallback=60`，`network_call_count=0`
  - run2：`completed=true`，`actual_windows=10`，`actual_turns=100`，`passed=false`，`exit_code=3`，`high=6`，`medium=2`，`fallback=60`，`network_call_count=0`
  - 两次 canonical hash 相同：`93ef3aaa709bf979b4e27d3793311e12fbdb1d17b5d2cb3ddd903d35c56e8cf3`

随机 QA 的 high / medium 是当前机器人业务质量问题，本轮没有降低门槛，也没有修改业务回复去消除它们。

## Artifact

- `qa_artifacts/m05c/repro_random_seed20260624.log`
- `qa_artifacts/m05c/path_diagnostics.txt`
- `qa_artifacts/m05c/targeted_new_tests_2.log`
- `qa_artifacts/m05c/compileall.log`
- `qa_artifacts/m05c/pytest_q.log`
- `qa_artifacts/m05c/qa_fixed_run1.log`
- `qa_artifacts/m05c/qa_fixed_run2.log`
- `qa_artifacts/m05c/qa_random_run1.log`
- `qa_artifacts/m05c/qa_random_run2.log`
- `qa_artifacts/m05c/random_compare.txt`
- `qa_artifacts/m05c/final_diff_ignore_cr.txt`

## 安全声明

- 未读取真实 `.env`
- 未访问公网
- 未连接飞书或企业微信
- 未 SSH
- 未部署
- 未进入 M1
