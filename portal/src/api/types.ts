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
export type Order = S["SalesOrderOut"];
export type OrderSummary = S["SalesOrderSummary"];
export type OrderPage = S["SalesOrderPage"];
export type OrderIn = S["SalesOrderIn"];
export type OrderEvent = S["OrderEventOut"];
export type OrderCounts = S["OrderCountsOut"];
export type OrderImport = S["OrderImportOut"];
export type StockRow = S["StockSummaryOut"];
export type StockSummary = S["StockSummaryPage"];
export type Site = S["SiteOut"];
export type SitePage = S["SitePage"];
export type Item = S["ItemOut"];
export type ItemPage = S["ItemPage"];
export type ClientCase = S["ClientExceptionOut"];
export type ClientCasePage = S["ClientExceptionPage"];
export type Notice = S["InboundNoticeOut"];
export type NoticePage = S["InboundNoticePage"];
export type NoticeIn = S["InboundNoticeIn"];
export type NoticeImport = S["AsnImportOut"];
export type Receipt = S["ReceiptOut"];
export type ReceiptPage = S["ReceiptPage"];
