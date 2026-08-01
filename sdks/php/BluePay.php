<?php
final class BluePayClient {
    public function __construct(
        private string $apiKey,
        private string $baseUrl,
        private int $timeoutSeconds = 15,
    ) { $this->baseUrl = rtrim($this->baseUrl, '/'); }

    private function request(string $path, string $method = 'GET', ?array $body = null, ?string $idempotencyKey = null): array {
        $headers = ['X-API-Key: ' . $this->apiKey, 'Content-Type: application/json', 'Accept: application/json'];
        if ($idempotencyKey) $headers[] = 'Idempotency-Key: ' . $idempotencyKey;
        $ch = curl_init($this->baseUrl . $path);
        curl_setopt_array($ch, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => $this->timeoutSeconds, CURLOPT_HTTPHEADER => $headers]);
        if ($body !== null) curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_UNICODE));
        $raw = curl_exec($ch);
        if ($raw === false) throw new RuntimeException(curl_error($ch));
        $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        $data = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
        if ($status < 200 || $status >= 300) throw new RuntimeException($data['error']['message'] ?? 'BluePay request failed', $status);
        return $data;
    }

    public function createInvoice(int $amountToman, string $orderId, ?string $description = null, string $feeMode = 'default'): array {
        return $this->request('/api/v1/invoices', 'POST', ['amount_toman' => $amountToman, 'order_id' => $orderId, 'description' => $description, 'fee_mode' => $feeMode], 'invoice-' . $orderId);
    }

    public function createSandboxInvoice(int $amountToman, string $orderId, ?string $description = null): array {
        return $this->request('/api/v1/sandbox/invoices', 'POST', ['amount_toman' => $amountToman, 'order_id' => $orderId, 'description' => $description], 'sandbox-' . $orderId);
    }
}
