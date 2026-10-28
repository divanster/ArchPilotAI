/**
 * The top bar's "Generate architecture" button lives outside the Architecture page, so the two
 * talk through these window events instead of sharing state.
 */
export const GENERATE_REQUEST = "archpilot:generate-architecture";
export const GENERATE_STATE = "archpilot:generating";

export function requestGenerate() {
  window.dispatchEvent(new Event(GENERATE_REQUEST));
}

export function announceGenerating(active: boolean) {
  window.dispatchEvent(new CustomEvent<boolean>(GENERATE_STATE, { detail: active }));
}
