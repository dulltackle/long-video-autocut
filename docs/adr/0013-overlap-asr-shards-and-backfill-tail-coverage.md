# 分片重叠 + 覆盖度兜底补转修复 ASR 尾部丢失

分片识别（见 [ADR 0004](0004-transcribe-long-live-videos-in-audio-shards.md)）真实端到端跑通后发现：某条拆条短视频字幕在片中出现约 15 秒空白。逐层排查确认根因在上游 ASR 分片而非字幕生成/烧录——StepAudio ASR 对长/密分片存在**确定性的尾部输出截断**：分片音频完整、尾部确有连续语音（`-30dB` 阈值下无静音），但转写文本只到分片内某处即停，尾部内容无 delta 返回；把该尾部音频单独送识别却能完整转出。全片 23 个分片中，分片 5/6/14 分别丢失约 15.4s / 164.6s / 14.9s，其中一片约 161s 连续语音被整段吞掉。

0004 的分片设计有两处放大了影响：相邻分片以 `start = end` 零重叠首尾相接，任一分片被截断的尾部不会被下一分片补回，直接成为永久缺口；且单分片只要返回非空 chunks 即判成功，不校验返回文本是否覆盖到分片音频时长，大段丢失被静默放过。本 ADR 在 0004 的分片框架上叠加两个协同机制，并保持转写产物结构、下游（候选/字幕/烧录）与缓存命名空间不变。

## Considered Options

- **机制 A — 分片重叠（降低边界摩擦）**：相邻分片在时间上重叠 `asr_shard_overlap_seconds` 秒，步进由 `start = end` 改为 `start = end - overlap`（即 `shard_seconds - overlap`），分片自身时长仍 `≤ shard_seconds`，上传预算（base64 后 ≤10MB）逻辑不变。前一分片被截断的尾部由后一分片**未截断的头部**重新覆盖，同时缓解跨边界断句；重叠区重复文本沿用既有 `_merge_overlapping_chunks` 按时间序合并去重，无需新去重逻辑。局限：只能覆盖 `overlap` 量级的小截断（默认 5s），**无法**独立兜住 165s 的灾难性丢失，故必须与机制 B 配合，是「常态小截断」的低成本防线。为避免步进退化，`overlap` 夹紧到 `[0, shard_seconds*0.5]`；末分片收口于时长末端即停，不产生零长/重复末分片。
- **机制 B — 覆盖度兜底补转（保证不丢大段）**：单分片转写后计算分片内已覆盖末端 `covered_end = max(chunk.end)`，若 `shard_audio_duration - covered_end > asr_coverage_gap_seconds`，则把尾部缺口 `[covered_end, shard_audio_duration]` 从分片 wav 切出**单独再转**、按 `covered_end` 偏移后并入本分片，循环直到缺口收敛、补转返回空/失败（视为静音）、或达到 `asr_coverage_max_passes` 上限。依据是「单独短音频可被完整转出」这一实测事实，故对 165s 的丢失也能分多轮补回。补转在分片内闭环，产出「补全后的分片 chunks」再写入分片缓存。
- **为什么两者都要**：只用 A 兜不住大段截断；只用 B 能兜大段但每片都要先探测缺口、边界断句仍粗糙。A 把常态小截断以近零成本吸收，B 作为大段丢失的确定性保障，互补而非二选一。
- **失败可观测**：补转达轮次上限仍有缺口时向 stderr 打印告警（含 shard 序号与剩余缺口秒数），不抛错、不中断整片，便于人工核查，取代此前的静默吞掉。`covered_end` 未推进即停，防病态死循环。

## Consequences

- 分片缓存签名（`_stepaudio_shard_cache_signature`）新增 `shard_overlap_ms` / `coverage_gap_ms` / `coverage_max_passes`，重叠或兜底参数变化时旧缓存自动失效；机制 A 改变分片边界，签名已含 `shard_start_ms/end_ms`，旧 `asr_shard_cache/` 自然失效并重转一次。这是修复必须付出的一次性 ASR 调用成本，重转后缓存即为补全版，后续复用不再有缺口。
- `stepaudio.json` / `transcript.json` 结构不变，下游候选生成、字幕、烧录无需改动。正常分片（无尾部丢失）：机制 B 因缺口 `≤ coverage_gap` 直接跳过，结果与现状一致，仅边界因重叠略有重叠区、已被合并去重吸收。
- 新增三个可调旋钮（默认 `asr_shard_overlap_seconds=5.0`、`asr_coverage_gap_seconds=3.0`、`asr_coverage_max_passes=4`）。回滚路径：`overlap=0` 且 `coverage_max_passes=0` 即退化为 0004 的原始行为，便于灰度与回退；额度成本由 `coverage_gap_seconds` / `coverage_max_passes` 控上限。
- `StepAudioConfig` dataclass 默认取「零重叠、不补转」以保持历史行为，生产默认值由 `config.py` 经 `create_stepaudio_transcriber` 透传；管线级 e2e 显式关闭重叠/兜底以固定分片场景，重叠与补转的行为由 `tests/test_transcript.py` 专项单测覆盖。
