// Response shapes, taken from the generated OpenAPI schema so a backend change that breaks the portal fails the
// type check. Regenerate with `npm run api-types` after changing an endpoint the portal uses.
import type { components } from "./schema";

type S = components["schemas"];

export type Session = S["SessionOut"];
export type ShipmentSummary = S["ShipmentSummary"];
export type ShipmentPage = S["ShipmentPage"];
export type Tracking = S["TrackingOut"];
export type Pod = S["PodOut"];
export type DocumentRef = S["DocumentRef"];
