# Local provider spend control

CVGnome records and controls metered model requests without a CVGnome cloud
backend. The first supported metered adapter is direct OpenAI BYOK. LM Studio
stays outside this ledger because it has no third-party per-token API charge;
the customer's hardware and electricity costs are not represented as zero.

## Scope and authority

The ledger covers inference requests sent by this CVGnome installation:

- the embedding and structured-generation steps in **Test OpenAI models**;
- the embedding and structured-generation steps in tailored-draft creation.

Model discovery is excluded because it lists account-visible IDs without
running inference. Local profile work, matching, revision editing, and export
remain excluded because they make no provider request.

Figures in CVGnome are local estimates, not provider invoices or account-wide
balances. They do not include calls made by other applications, devices, keys,
or earlier CVGnome versions. OpenAI exposes account reconciliation through its
[organization Usage and Costs
APIs](https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/usage),
which use an administrative credential; CVGnome deliberately does not request
or store that broader credential. The provider dashboard remains authoritative.

## Prices and limits

The model-list response does not provide a stable CVGnome price contract.
CVGnome therefore never invents a price or silently updates one. The customer
enters the current standard input and output price for each exact model ID in
USD per one million tokens. A dated model ID and an alias are separate cards.

Tracking is available while the guard is off. If a matching price card exists,
CVGnome estimates the charge; otherwise it records the provider-reported usage
as unpriced. Turning the guard on makes a matching price card mandatory and can
enforce either or both of:

- a ceiling for each individual embedding or generation request;
- a cumulative calendar-month ceiling.

The monthly period is derived by native code rather than supplied by the
renderer. Its timezone is shown in the Models workspace. Limits apply only to
the local CVGnome ledger and are checked before the request is dispatched.

## Reservation and reconciliation

Before a guarded or monitored cloud request leaves the computer, native code
creates an owner-only durable reservation. The conservative input-token upper
bound is the bounded UTF-8 JSON request size, and the output bound is the
explicit provider output-token ceiling (zero for embeddings). The selected
price card turns those bounds into a worst-case local cost estimate. A request
that would cross an enabled per-request or monthly limit is not sent.

OpenAI [embedding
responses](https://developers.openai.com/api/reference/resources/embeddings/methods/create)
report `prompt_tokens` and `total_tokens`. [Responses API
results](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
report top-level `input_tokens`, `output_tokens`, and `total_tokens`; output
includes reasoning tokens. CVGnome charges all reported input and output tokens
at the customer-entered standard rates, conservatively ignoring any
cached-input discount. When valid usage arrives, the reservation becomes a
completed estimate. If the provider reports more tokens than the conservative
reservation allowed, CVGnome records the larger known amount before rejecting
the model result.

A timeout, interrupted process, or missing or malformed usage receipt can leave
it unclear whether the provider billed the call. CVGnome retains the reserved
upper bound as uncertain rather than recording zero; an interrupted reservation
is reconciled at the next serialized provider or spend-control boundary. When
usage is valid but the returned vectors or structured content are not, CVGnome
records the known token estimate even though it rejects the model result. These
amounts continue to count toward the local monthly ceiling. There is no
automatic retry, provider fallback, or silent release of an ambiguous
reservation.

Completed and uncertain requests are accumulated into bounded monthly rollups
before old detail rows are pruned, so pruning recent activity cannot reduce an
enforced month total. Settings, rollups, and recent activity are stored in
`provider-spend.json` in the application-data directory using an atomic
owner-only file replacement. The API key is never written there.

The cloud-cost ledger is operating configuration, not career material. **Reset
career workspace** therefore preserves it along with model configuration and
the operating-system credential entry.
