export interface Policy {
  id: number
  policy_no: string
  name: string
  scope: string
  scope_value: string | null
  monthly_budget_cny: number
  unit_cost_per_gib_cny: number
  warn_ratio: number
  critical_ratio: number
  scan_alert_bytes: number
  is_active: boolean
  note: string
  updated_by: string | null
}

export interface TableScanRow {
  table_name: string
  layer: string
  scan_bytes: number
  query_count: number
  blocked_count: number
}

export interface BudgetSavingRow {
  suggestion_no: string
  query_external_id: string
  target_table: string | null
  status: 'pending' | 'adopted' | 'rejected'
  saving_bytes: number
  saving_cost_cny: number
  monthly_saving_cny: number
  estimate_run_no: string | null
}

export interface BudgetOverview {
  monthly_budget_cny: number
  /** 最近一次全量批次冻结的月折算原费用（改写前） */
  original_cost_cny: number
  /** 已采纳建议在该批次的冻结月节省 */
  confirmed_saving_cny: number
  /** 待采纳建议在该批次的冻结月节省 */
  pending_saving_cny: number
  /** 原费用 − 已确认节省（待确认节省只做参考） */
  adjusted_cost_cny: number
  /** 兼容字段：= adjusted_cost_cny */
  projected_cost_cny: number
  month_share: number
  status: 'ok' | 'warn' | 'critical' | 'over'
  confirmed_share: number
  pending_share: number
  unit_cost_per_gib_cny: number
  alert_bytes: number
  total_scan_bytes: number
  query_count: number
  blocked_count: number
  estimate_run_no: string | null
  estimate_run_finished_at: string | null
  prunable_saving_bytes: number
  suggestion_count: number
  confirmed_suggestion_count: number
  pending_suggestion_count: number
  savings: BudgetSavingRow[]
  tables: TableScanRow[]
  policies: Policy[]
}

export interface AuditLog {
  id: number
  actor_email: string
  actor_role: string
  action: string
  resource_type: string
  resource_id: string | null
  method: string
  path: string
  status_code: number
  created_at: string
}
