export interface EstimateRun {
  id: number
  run_no: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  scope: string
  scope_value: string | null
  query_count: number
  parsed_count: number
  blocked_count: number
  prunable_count: number
  total_scan_bytes: number
  total_cost_cny: number
  monthly_projected_cost_cny: number
  /** 该批次冻结的建议收益（每条建议只计一次） */
  saved_by_rewrites_bytes: number
  saved_by_rewrites_cost_cny: number
  suggestion_count: number
  top_query_id: number | null
  top_scan_bytes: number
  budget_status: string
  note: string
  error_message: string | null
  created_by: string
  started_at: string | null
  finished_at: string | null
}
