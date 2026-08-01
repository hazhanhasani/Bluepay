export class BluePayClient {
  constructor({ apiKey, baseUrl, timeoutMs = 15000 }) {
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
          "Content-Type": "application/json",
          ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await response.json();
      if (!response.ok) throw Object.assign(new Error(data?.error?.message || "BluePay request failed"), { response, data });
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  createInvoice({ amountToman, orderId, description, feeMode = "default" }) {
    return this.request("/api/v1/invoices", {
      method: "POST",
      idempotencyKey: `invoice-${orderId}`,
      body: { amount_toman: amountToman, order_id: orderId, description, fee_mode: feeMode },
    });
  }

  createSandboxInvoice({ amountToman, orderId, description }) {
    return this.request("/api/v1/sandbox/invoices", {
      method: "POST",
      idempotencyKey: `sandbox-${orderId}`,
      body: { amount_toman: amountToman, order_id: orderId, description },
    });
  }
}
