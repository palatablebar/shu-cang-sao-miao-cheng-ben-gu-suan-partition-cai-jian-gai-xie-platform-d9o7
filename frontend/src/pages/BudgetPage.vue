<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/authStore'
import { useBudgetStore } from '../stores/budgetStore'
import { useEstimateStore } from '../stores/estimateStore'
import { useFeedback } from '../hooks/useFeedback'
import BlueprintSheet from '../components/common/BlueprintSheet.vue'
import BlueprintPanel from '../components/common/BlueprintPanel.vue'
import ErrorNotice from '../components/common/ErrorNotice.vue'
import MonoMetric from '../components/common/MonoMetric.vue'
import StatusStamp from '../components/common/StatusStamp.vue'
import { BUDGET_STATUS_LABEL, RUN_STATUS_LABEL, SUGGESTION_STATUS_LABEL } from '../constants/enums'
import { cny, dateLabel, gibLabel, intLabel, ratio } from '../utils/format'

const auth = useAuthStore()
const store = useBudgetStore()
const estimates = useEstimateStore()
const feedback = useFeedback()

const editingId = ref<number | null>(null)
const budgetInput = ref(0)
const warnInput = ref(0.75)
const busy = ref(false)

const overview = computed(() => store.overview)
// 仪表盘刻度取「原费用 / 预算」的较大值：绿色 = 采纳后的实际占用，斜纹 = 采纳待确认建议后
// 还能回落的区间；warn / critical 刻度始终按预算比例落位。
const scale = computed(() =>
  Math.max(1, overview.value?.original_cost_cny ?? 0, overview.value?.monthly_budget_cny ?? 0),
)
const adjustedPct = computed(() => Math.min(100, ((overview.value?.adjusted_cost_cny ?? 0) / scale.value) * 100))
const pendingPct = computed(() =>
  Math.min(100 - adjustedPct.value, ((overview.value?.pending_saving_cny ?? 0) / scale.value) * 100),
)
const budgetScale = computed(() => (overview.value?.monthly_budget_cny ?? 0) / scale.value)
const globalPolicy = computed(() => store.policies.find((policy) => policy.scope === 'global'))
const frozenAt = computed(() => overview.value?.estimate_run_finished_at ?? '')

function startEdit(id: number) {
  const policy = store.policies.find((item) => item.id === id)
  if (!policy) return
  editingId.value = id
  budgetInput.value = policy.monthly_budget_cny
  warnInput.value = policy.warn_ratio
}

async function save(id: number) {
  busy.value = true
  try {
    await store.savePolicy(id, {
      monthly_budget_cny: Number(budgetInput.value),
      warn_ratio: Number(warnInput.value),
    })
    editingId.value = null
    feedback.notify('预算策略已更新（审计已记录）', 'ok')
  } catch (err) {
    feedback.notify((err as Error).message, 'bad')
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await Promise.all([store.load(), estimates.load()])
  if (auth.isAdmin) await store.loadAudit()
})
</script>

<template>
  <div class="page">
    <BlueprintSheet
      title="预算策略与估算批次"
      code="S-05 / BUDGET PLATE"
      note="费用口径 = 最近一次全量批次冻结的单日扫描 × 30 天 × 5 倍放样；收益按建议首次计入的批次只算一次。">
      <ErrorNotice v-if="store.error" :message="store.error" @retry="store.load()" />

      <template v-if="overview">
        <div class="top">
          <div class="metrics">
            <MonoMetric :value="cny(overview.monthly_budget_cny, 0)" caption="月预算 / BUDGET" />
            <MonoMetric :value="cny(overview.original_cost_cny, 0)" caption="原费用（改写前）/ ORIGINAL" />
            <MonoMetric :value="cny(overview.confirmed_saving_cny, 0)" caption="已确认节省 / CONFIRMED" />
            <MonoMetric :value="cny(overview.pending_saving_cny, 0)" caption="待确认节省 / PENDING" />
          </div>

          <div class="gauge">
            <div class="gauge-head">
              <StatusStamp :label="BUDGET_STATUS_LABEL[overview.status] || overview.status" :level="overview.status" />
              <span class="mono hint">阈值 warn {{ ratio(globalPolicy?.warn_ratio ?? 0.75, 2) }} / critical {{ ratio(globalPolicy?.critical_ratio ?? 0.92, 2) }}</span>
            </div>
            <div class="gauge-track">
              <span class="gauge-saving" :style="{ width: `${adjustedPct}%` }" />
              <span class="gauge-extra" :style="{ left: `${adjustedPct}%`, width: `${pendingPct}%` }" />
              <span class="gauge-mark" :style="{ left: `${budgetScale * (globalPolicy?.warn_ratio ?? 0.75) * 100}%` }" />
              <span class="gauge-mark gauge-mark--critical" :style="{ left: `${budgetScale * (globalPolicy?.critical_ratio ?? 0.92) * 100}%` }" />
            </div>
            <div class="gauge-axis mono">
              <span>0</span><span>warn</span><span>critical</span><span>{{ cny(overview.monthly_budget_cny, 0) }}</span>
            </div>
            <p class="hint" style="margin: 6px 0 0">
              <span class="gauge-legend gauge-legend--confirmed" />调整后预计
              <span class="mono">{{ cny(overview.adjusted_cost_cny, 0) }}</span>（预算占用
              {{ ratio(overview.month_share, 3) }}） ·
              <span v-if="overview.pending_saving_cny > 0">
                <span class="gauge-legend" />待确认节省
                <span class="mono">{{ cny(overview.pending_saving_cny, 0) }}</span>（全部采纳可降至
                <span class="mono">{{ cny(Math.max(0, overview.adjusted_cost_cny - overview.pending_saving_cny), 0) }}</span>，
                占用 {{ ratio((Math.max(0, overview.adjusted_cost_cny - overview.pending_saving_cny)) / Math.max(1, overview.monthly_budget_cny), 3) }}）
              </span>
            </p>
            <div class="cost-ladder">
              <div class="cost-row">
                <span class="cost-label">原费用</span>
                <span class="mono">{{ cny(overview.original_cost_cny, 2) }}</span>
              </div>
              <div class="cost-row cost-row--minus">
                <span class="cost-label">− 已确认节省（{{ overview.confirmed_suggestion_count }} 条已采纳）</span>
                <span class="mono">{{ cny(overview.confirmed_saving_cny, 2) }}</span>
              </div>
              <div class="cost-row cost-row--adjusted">
                <span class="cost-label">= 调整后预计费用</span>
                <span class="mono">{{ cny(overview.adjusted_cost_cny, 2) }}</span>
              </div>
              <div class="cost-row cost-row--pending">
                <span class="cost-label">待确认节省（{{ overview.pending_suggestion_count }} 条待采纳，未抵扣）</span>
                <span class="mono">{{ cny(overview.pending_saving_cny, 2) }}</span>
              </div>
            </div>
            <p class="hint" style="margin: 10px 0 0">
              批次 <span class="mono">{{ overview.estimate_run_no || '—' }}</span>
              <span v-if="frozenAt">（{{ dateLabel(frozenAt) }} 冻结）</span> ·
              查询 {{ intLabel(overview.query_count) }} 条 · 裁剪失效 {{ intLabel(overview.blocked_count) }} 条 ·
              单日扫描 <span class="mono">{{ gibLabel(overview.total_scan_bytes) }}</span>
            </p>
          </div>
        </div>

        <div class="grid">
          <BlueprintPanel label="建议收益（批次冻结口径）/ SAVINGS">
            <table class="grid-table">
              <thead>
                <tr><th>建议</th><th>查询</th><th class="num">状态</th><th class="num">单次节省</th><th class="num">月节省</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in overview.savings" :key="row.suggestion_no">
                  <td class="mono">{{ row.suggestion_no }}</td>
                  <td class="mono hint">{{ row.query_external_id }} · {{ row.target_table || '-' }}</td>
                  <td class="num">
                    <StatusStamp :label="SUGGESTION_STATUS_LABEL[row.status] || row.status" :level="row.status" />
                  </td>
                  <td class="num mono">{{ cny(row.saving_cost_cny, 3) }}</td>
                  <td class="num mono" :style="{ color: row.status === 'adopted' ? 'var(--ok, inherit)' : 'inherit' }">
                    {{ cny(row.monthly_saving_cny, 2) }}
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!overview.savings.length" class="hint" style="margin: 8px 0 0">
              该批次还没有计入任何改写建议；生成/采纳建议后只会在这一个批次里计一次收益。
            </p>
          </BlueprintPanel>

          <BlueprintPanel label="按表扫描量 / SCAN BY TABLE">
            <table class="grid-table">
              <thead>
                <tr><th>表</th><th>层</th><th class="num">查询</th><th class="num">失效</th><th class="num">扫描字节</th><th class="num">占比</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in overview.tables" :key="row.table_name">
                  <td class="mono">{{ row.table_name }}</td>
                  <td><span class="tag">{{ row.layer }}</span></td>
                  <td class="num mono">{{ row.query_count }}</td>
                  <td class="num mono" :style="{ color: row.blocked_count ? 'var(--bad)' : 'inherit' }">{{ row.blocked_count }}</td>
                  <td class="num mono">{{ gibLabel(row.scan_bytes) }}</td>
                  <td class="num mono">{{ ratio(row.scan_bytes / Math.max(1, overview.total_scan_bytes), 3) }}</td>
                </tr>
              </tbody>
            </table>
          </BlueprintPanel>

          <BlueprintPanel label="估算批次 / RUNS">
            <table class="grid-table">
              <thead>
                <tr><th>批次</th><th>状态</th><th class="num">查询</th><th class="num">失效</th><th class="num">Top1</th><th class="num">月折算</th><th class="num">建议收益</th></tr>
              </thead>
              <tbody>
                <tr v-for="run in estimates.runs" :key="run.id">
                  <td>
                    <p class="mono" style="margin: 0">{{ run.run_no }}</p>
                    <p class="meta">{{ run.note || run.scope }}</p>
                  </td>
                  <td><StatusStamp :label="RUN_STATUS_LABEL[run.status] || run.status" :level="run.status" /></td>
                  <td class="num mono">{{ run.query_count }}</td>
                  <td class="num mono">{{ run.blocked_count }}</td>
                  <td class="num mono">{{ gibLabel(run.top_scan_bytes) }}</td>
                  <td class="num mono">{{ cny(run.monthly_projected_cost_cny, 0) }}</td>
                  <td class="num mono">{{ cny(run.saved_by_rewrites_cost_cny, 2) }}</td>
                </tr>
              </tbody>
            </table>
            <p class="hint" style="margin: 10px 0 0">
              专项批次（按表 / 仅失效复核）只冻结扫描结论，不重复计入建议收益；历史批次不会因日志重导或分区元数据变化而改写。
            </p>
          </BlueprintPanel>
        </div>

        <BlueprintPanel label="预算策略 / POLICIES">
          <table class="grid-table">
            <thead>
              <tr>
                <th>编号 / 名称</th>
                <th>范围</th>
                <th class="num">月预算</th>
                <th class="num">warn / critical</th>
                <th class="num">单条告警阈值</th>
                <th>维护</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="policy in store.policies" :key="policy.id">
                <td>
                  <p class="mono" style="margin: 0">{{ policy.policy_no }}</p>
                  <p class="meta">{{ policy.name }}</p>
                </td>
                <td><span class="tag">{{ policy.scope }}{{ policy.scope_value ? ` · ${policy.scope_value}` : '' }}</span></td>
                <td class="num mono">
                  <template v-if="editingId === policy.id">
                    <input v-model.number="budgetInput" class="inline-input" type="number" step="100" />
                  </template>
                  <template v-else>{{ cny(policy.monthly_budget_cny, 0) }}</template>
                </td>
                <td class="num mono">
                  <template v-if="editingId === policy.id">
                    <input v-model.number="warnInput" class="inline-input" type="number" step="0.01" min="0.1" max="0.99" />
                    / {{ ratio(policy.critical_ratio, 2) }}
                  </template>
                  <template v-else>{{ ratio(policy.warn_ratio, 2) }} / {{ ratio(policy.critical_ratio, 2) }}</template>
                </td>
                <td class="num mono">{{ gibLabel(policy.scan_alert_bytes, 1) }}</td>
                <td>
                  <template v-if="auth.isAdmin">
                    <button v-if="editingId !== policy.id" class="btn btn--ghost" type="button" @click="startEdit(policy.id)">编辑</button>
                    <span v-else class="row">
                      <button class="btn btn--primary" type="button" :disabled="busy" @click="save(policy.id)">保存</button>
                      <button class="btn btn--ghost" type="button" @click="editingId = null">取消</button>
                    </span>
                  </template>
                  <span v-else class="hint">仅 admin 可改</span>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="hint" style="margin: 10px 0 0">
            维护人：<span class="mono">{{ globalPolicy?.updated_by || '—' }}</span> · 只读角色请求 PUT /budget/policies 会拿到真实 403。
          </p>
        </BlueprintPanel>

        <BlueprintPanel v-if="auth.isAdmin && store.audit.length" label="审计流水 / AUDIT TRAIL（最近 14 条）">
          <table class="grid-table">
            <thead>
              <tr><th>时间</th><th>操作者</th><th>角色</th><th>动作</th><th>路径</th><th class="num">状态</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in store.audit" :key="row.id">
                <td class="mono hint">{{ dateLabel(row.created_at) }}</td>
                <td class="mono">{{ row.actor_email }}</td>
                <td><span class="tag">{{ row.actor_role }}</span></td>
                <td class="mono">{{ row.action }}</td>
                <td class="mono hint">{{ row.method }} {{ row.path }}</td>
                <td class="num mono" :style="{ color: row.status_code >= 400 ? 'var(--bad)' : 'inherit' }">{{ row.status_code }}</td>
              </tr>
            </tbody>
          </table>
        </BlueprintPanel>
      </template>
    </BlueprintSheet>
  </div>
</template>

<style scoped>
.top {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: var(--space-5);
  border-bottom: 1px solid var(--rule);
  padding-bottom: var(--space-4);
  margin-bottom: var(--space-4);
}
.metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-5);
}
.gauge-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.gauge-track {
  position: relative;
  height: 14px;
  border: 1px solid var(--rule);
  background: oklch(0.22 0.045 254 / 0.9);
}
.gauge-saving {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  background: linear-gradient(90deg, oklch(0.68 0.09 150), oklch(0.74 0.1 120));
}
.gauge-extra {
  position: absolute;
  top: 0;
  height: 100%;
  background: repeating-linear-gradient(
    135deg,
    oklch(0.74 0.1 120 / 0.55) 0 4px,
    oklch(0.30 0.05 250 / 0.7) 4px 8px
  );
}
.cost-ladder {
  margin-top: var(--space-3);
  border-top: 1px solid var(--rule);
}
.cost-row {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  padding: 5px 0;
  border-bottom: 1px dashed var(--rule);
  font-size: 0.72rem;
}
.cost-label {
  color: var(--cream-mute);
}
.cost-row--adjusted {
  border-bottom: 1px solid var(--rule-strong);
  color: var(--sand);
  font-weight: 600;
}
.cost-row--pending {
  color: var(--cream-mute);
}
.gauge-mark {
  position: absolute;
  top: -4px;
  width: 1px;
  height: 20px;
  background: var(--warn);
}
.gauge-mark--critical {
  background: var(--bad);
}
.gauge-axis {
  display: flex;
  justify-content: space-between;
  font-size: 0.64rem;
  color: var(--cream-mute);
  margin-top: 6px;
}
.gauge-legend {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-right: 4px;
  vertical-align: 0;
  background: repeating-linear-gradient(
    135deg,
    oklch(0.74 0.1 120 / 0.55) 0 2px,
    oklch(0.30 0.05 250 / 0.7) 2px 4px
  );
  border: 1px solid var(--rule);
}
.gauge-legend--confirmed {
  background: oklch(0.70 0.09 140);
}
.grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--space-4);
}
.meta {
  margin: 2px 0 0;
  font-size: 0.66rem;
  color: var(--cream-mute);
}
.inline-input {
  width: 100px;
  background: oklch(0.225 0.045 254 / 0.9);
  border: 1px solid var(--rule-strong);
  padding: 3px 6px;
  font-family: var(--font-mono);
  font-size: 0.72rem;
  text-align: right;
}
@media (max-width: 1080px) {
  .top {
    grid-template-columns: 1fr;
  }
}
</style>
