export class BluePayClient {
  constructor({ apiKey, baseUrl, timeoutMs = 15000 }) {
    if (!apiKey) throw new Error("apiKey is required");
    if (!baseUrl) throw new Error("baseUrl is required");
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.timeoutMs = timeoutMs;
  }

  async request(path, { method = "GET", body, idempotencyKey } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        method,
        signal: controller.signal,
        headers: {
          "X-API-Key": this.apiKey,
          "Accept": "application/json",
          "Content-Type": "application/json",
          ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw Object.assign(new Error(data?.error?.message || data?.detail || "BluePay request failed"), {
          status: response.status,
          response,
          data,
        });
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  createInvoice({ amountToman, orderId, description, feeMode = "default", callbackUrl, ttlMinutes, idempotencyKey }) {
    return this.request("/api/v1/invoices", {
      method: "POST",
      idempotencyKey: idempotencyKey || `invoice-${orderId}`,
      body: {
        amount_toman: amountToman,
        order_id: orderId,
        description,
        fee_mode: feeMode,
        ...(callbackUrl ? { callback_url: callbackUrl } : {}),
        ...(ttlMinutes !== undefined ? { ttl_minutes: ttlMinutes } : {}),
      },
    });
  }

  getInvoice(paymentId) {
    return this.request(`/api/v1/invoices/${encodeURIComponent(paymentId)}`);
  }

  getInvoiceTimeline(paymentId) {
    return this.request(`/api/v1/invoices/${encodeURIComponent(paymentId)}/timeline`);
  }

  cancelInvoice(paymentId, { idempotencyKey } = {}) {
    return this.request(`/api/v1/invoices/${encodeURIComponent(paymentId)}/cancel`, {
      method: "POST",
      idempotencyKey: idempotencyKey || `cancel-${paymentId}`,
    });
  }

  createSandboxInvoice({ amountToman, orderId, description, idempotencyKey }) {
    return this.request("/api/v1/sandbox/invoices", {
      method: "POST",
      idempotencyKey: idempotencyKey || `sandbox-${orderId}`,
      body: { amount_toman: amountToman, order_id: orderId, description },
    });
  }

  getSandboxInvoice(paymentId) {
    return this.request(`/api/v1/sandbox/invoices/${encodeURIComponent(paymentId)}`);
  }

  simulateSandbox(paymentId, { result = "paid", referenceNumber } = {}) {
    return this.request(`/api/v1/sandbox/invoices/${encodeURIComponent(paymentId)}/simulate`, {
      method: "POST",
      idempotencyKey: `sandbox-simulate-${paymentId}-${result}`,
      body: {
        result,
        ...(result === "paid" && referenceNumber ? { reference_number: referenceNumber } : {}),
      },
    });
  }
}
