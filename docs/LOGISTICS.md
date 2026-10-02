# Transport & logistics in AgriMarket

This note explains how transport works today: the data model, the route
optimisation, the delivery fee, how to use it, and what is still missing.

---

## 1. What "transport" means here

AgriMarket does **not** own trucks. It **plans and orchestrates** movement:

1. A buyer orders from a farmer.
2. An operator (staff) bundles one or more orders into a **shipment**.
3. The shipment becomes an **optimised multi-stop route**: pick up from each
   farmer, deliver to each buyer, in the cheapest sensible order.
4. A **delivery partner** (driver / fleet) is assigned to run it, and the
   delivery cost is computed and charged to the buyer.

This is the "logistics support" the project promises — it turns a pile of
individual orders into a single efficient trip instead of each buyer travelling
to each farm.

---

## 2. Data model

| Model | Purpose |
| --- | --- |
| `DeliveryPartner` | A driver/fleet: name, phone, vehicle type (bike / tempo / truck / refrigerated), capacity (kg), availability, current location, optional linked user |
| `Shipment` | One trip: optional partner, status (planned / dispatched / completed / cancelled), planned distance, planned cost, route notes, optimised-at timestamp |
| `ShipmentStop` | An ordered stop: kind (`pickup` or `delivery`), sequence, label, coordinates, status, completion time, optional link to its `Order` |

A shipment with 3 orders therefore has **6 stops** (3 pickups + 3 deliveries),
each carrying its own coordinates and position in the route.

---

## 3. The optimiser (`marketplace/services/routing.py`)

Dependency-free geometry — no API key, works offline:

- `haversine_km(a, b)` — great-circle distance between two `(lat, lng)` points.
- `path_distance(points)` — total length of an ordered path.
- `nearest_neighbor_route(...)` — greedy: always go to the closest unvisited stop.
- `two_opt_improve(...)` — uncrosses the route until no swap shortens it.
- `optimize_stops(origin, stops)` — runs both and returns the visit order, total
  km, and per-leg distances.

Nearest-neighbour gives a fast first answer; 2-opt cleans it up. For the handful
of stops in a farm-to-buyer run this is near-optimal and instant.

---

## 4. Planning a shipment (`marketplace/services/logistics.py`)

`plan_shipment(order_ids, partner=None)` does the whole job in one transaction:

1. **Build stops** — for each order: a `pickup` at the farmer's coordinates and a
   `delivery` at `order.delivery_latitude/longitude` (falling back to the buyer's
   profile point).
2. **Choose an origin** — the partner's current location if known, else the first
   pickup.
3. **Optimise** — geometric baseline via `optimize_stops`; if AI is configured it
   proposes an order too, and the proposal is accepted **only if it measures
   strictly shorter**. Stops are re-sequenced and `planned_distance_km` is set.
4. **Price it** — `estimate_delivery_fee(distance)` = `DELIVERY_BASE_FEE` +
   `DELIVERY_PER_KM_FEE × km` (defaults ₹25 + ₹6/km, env-configurable).
5. **Charge it** — the delivery fee is split across the shipment's orders, and each
   order gets `pickup_hub` set to the farmer's village.

Orders without coordinates are still added as stops, but are excluded from the
distance calculation (see the gaps in §7).

---

## 5. How to use it

### Prerequisites

Farmer and buyer profiles need `latitude`/`longitude`. Set them in the Django
admin (or extend your signup form to capture a map pin / geocode the address).

### Web (staff only)

1. Log in as a staff user and open **Logistics** (`/shipments/`).
2. Under **Plan a shipment**, tick the orders to bundle.
3. Click **Optimise & create shipment**.
4. Open the shipment to see the ordered stops, total km, and planned cost.

### API

```bash
POST /api/shipments/plan/          # staff only
{ "order_ids": [1, 2, 3], "partner_id": 1 }
```

Returns the created shipment with its `stops` array in visiting order.

### Delivery fee example

A 40 km route → `25 + 6 × 40` = **₹265**, split across the bundled orders.

---

## 6. Who can see what

| Role | Sees |
| --- | --- |
| Staff | All shipments, and the planning form |
| Delivery partner | Shipments assigned to them |
| Farmer | Shipments containing their orders |
| Buyer | Shipments containing their orders |

`ShipmentListView` and `ShipmentViewSet` apply these scopes; `DeliveryPartner`
records are managed by staff only.

---

## 7. What is NOT built yet (read this)

The routing and costing core is real, but transport is the least "production
hardened" area. Missing pieces:

| Not done | Impact | Fix |
| --- | --- | --- |
| **Road distance vs straight-line** | Haversine underestimates real driving km, so costs run low | Use a distance-matrix API (OSRM self-hosted, OpenRouteService, Google) |
| **Geocoding** | Coordinates must be entered by hand; addresses aren't converted | Geocode addresses at signup/admin via Nominatim/Mapbox/Google |
| **Live GPS tracking** | `DeliveryPartner.current_latitude/longitude` exist but nothing updates them, and buyers have no map to watch | Add a partner location ping endpoint + a buyer tracking page |
| **Vehicle capacity not enforced** | `capacity_kg` is stored but orders are bundled regardless of weight | Refuse to bundle when total weight exceeds capacity; split shipments |
| **Manual partner assignment** | An operator picks the partner; no matching by proximity/availability | Auto-match the nearest available partner that fits the load |
| **Fee split is even, not per-leg** | Two orders 5 km and 40 km apart pay the same | Allocate by each order's own leg distance |
| **Shipment/stop status flow not enforced** | `status` and `completed_at` are free-form (admin edits) | Add transition validation + per-stop timestamps and a dispatch/complete flow |
| **No proof of delivery** | Nothing proves goods arrived | OTP or photo/signature capture at the delivery stop |
| **No time windows** | Orders can't be scheduled for a pickup/delivery slot | Add slot fields and a time-constrained optimiser |
| **No partner portal** | `DeliveryPartner.user` is linked but there's no dedicated UI | Build accept/dispatch/complete screens for partners |
| **No 3rd-party last mile** | No Porter/Shadowfax/Delhivery integration | Add a provider adapter for outsourced legs |
| **Cold chain is only a label** | "refrigerated" is a vehicle type, not enforced | Only assign refrigerated vehicles to perishables, track temperature |

Fixing the first three (road distance, geocoding, tracking) would move transport
from "demo-ready" to "operational". See `docs/ROADMAP.md` for the ordered plan.

---

## 8. Testing

```bash
python manage.py test marketplace.tests.test_services.RoutingTests
python manage.py test marketplace.tests.test_services.LogisticsTests
```

`RoutingTests` checks distances and that the optimiser returns a valid, shorter
permutation. `LogisticsTests` plans a real shipment and asserts stops, distance,
and the delivery fee. All offline — no maps or API keys required.
