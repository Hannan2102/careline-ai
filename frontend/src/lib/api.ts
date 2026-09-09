/**
 * The backend API, typed.
 *
 * These types are hand-written mirrors of the FastAPI response models rather
 * than generated from OpenAPI: there are eight of them, generation would add a
 * build step and a checked-in artefact, and a mismatch shows up immediately in
 * the pages that render them. If this surface grows, generate it.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      `Cannot reach the API at ${API_BASE}. Is the backend running (\`make dev\`)?`,
      0,
    );
  }
  if (!response.ok) {
    const detail = await response
      .json()
      .then((body: { detail?: string }) => body.detail)
      .catch(() => undefined);
    throw new ApiError(detail ?? `${response.status} ${response.statusText}`, response.status);
  }
  return (await response.json()) as T;
}

// ---------------------------------------------------------------- system

export interface SystemStatus {
  status: string;
  version: string;
  environment: string;
  synthetic_data_only: boolean;
  ehr: { provider: string; reachable: boolean; fhir_base_url: string | null };
  ai: {
    mode: string;
    llm_provider: string;
    stt_provider: string;
    tts_provider: string;
    paid_providers_selected: string[];
    can_spend_money: boolean;
  };
  modes: Record<string, boolean>;
}

export interface UsageSummary {
  estimated_cost_today_usd: string;
  estimated_project_cost_usd: string;
  project_limit_usd: string;
  warn_threshold_usd: string;
  remaining_usd: string;
  status: string;
  paid_calls_allowed: boolean;
  can_spend_money: boolean;
  by_provider: Record<string, string>;
}

export interface Overview {
  window_hours: number;
  generated_at: string;
  calls: number;
  turns: number;
  escalations: number;
  average_turn_ms: number;
  refused_turns: number;
  turns_by_intent: Record<string, number>;
  actions: Record<string, number>;
  usage: UsageSummary;
  synthetic_data_only: boolean;
}

// ----------------------------------------------------------------- calls

export interface CallSummary {
  session_id: string;
  channel: string;
  verification: string;
  patient_ref: string | null;
  patient_name: string | null;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  turns: number;
  escalations: number;
  last_intent: string | null;
  workflow: string | null;
  estimated_cost_usd: string;
  outcome: string;
}

export interface Operation {
  event_id: string;
  action: string;
  outcome: string;
  patient_ref: string | null;
  resource_type: string | null;
  resource_id: string | null;
  detail: string | null;
  created_at: string;
}

export interface TurnDetail {
  turn_id: string;
  turn_number: number;
  created_at: string;
  utterance: string;
  response: string;
  safety_outcome: string;
  safety_category: string | null;
  safety_rule: string | null;
  intent: string;
  confidence: number;
  entities: Record<string, string>;
  workflow: string | null;
  workflow_state: string | null;
  workflow_status: string | null;
  escalation_id: string | null;
  verification_state: string;
  safety_ms: number;
  extraction_ms: number;
  workflow_ms: number;
  total_ms: number;
  stt_ms: number | null;
  tts_first_audio_ms: number | null;
  estimated_cost_usd: string;
  operations: Operation[];
}

export interface CallTrace {
  call: CallSummary;
  turns: TurnDetail[];
  unattributed_operations: Operation[];
}

// --------------------------------------------------------------- records

export interface PatientView {
  reference: string;
  full_name: string;
  date_of_birth: string;
  phone: string | null;
  email: string | null;
  postal_code: string | null;
  synthetic: boolean;
}

export interface MedicationView {
  medication_request_id: string;
  display_name: string;
  dosage_instruction: string | null;
  status: string;
  prescriber_name: string | null;
  refills_remaining: number | null;
}

export interface AppointmentView {
  appointment_id: string;
  status: string;
  appointment_type: string;
  start: string;
  end: string;
  duration_minutes: number;
  patient_ref: string;
  practitioner_ref: string;
  practitioner_name: string;
  reason: string | null;
}

export interface PatientDetail {
  patient: PatientView;
  medications: MedicationView[];
  appointments: AppointmentView[];
  conditions: { condition_id: string; display_name: string; clinical_status: string }[];
  allergies: {
    allergy_id: string;
    substance: string;
    reaction: string | null;
    criticality: string | null;
  }[];
}

export interface PractitionerView {
  reference: string;
  display_name: string;
  specialty: string;
}

// ----------------------------------------------------- the work queues

export interface EscalationView {
  escalation_id: string;
  category: string;
  priority: string;
  destination: string;
  summary: string;
  patient_ref: string | null;
  verification_state: string;
  medication_display: string | null;
  patient_question: string | null;
  ai_action: string;
  session_id: string | null;
  created_at: string;
}

export interface RefillRequestView {
  refill_request_id: string;
  patient_ref: string;
  medication_request_id: string;
  medication_display: string;
  status: string;
  requested_at: string;
  session_id: string | null;
}
