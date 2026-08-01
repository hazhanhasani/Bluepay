<?php
declare(strict_types=1);

final class BluePayClient
{
    public function __construct(
        private string $apiKey,
        private string $baseUrl,
        private int $timeoutSeconds = 15,
    ) {
        if ($this->apiKey === '') throw new InvalidArgumentException('apiKey is required');
        $this->baseUrl = rtrim($this->baseUrl, '/');
    }

    private function request(
        string $path,
        string $method = 'GET',
        ?array $body = null,
        ?string $idempotencyKey = null,
    ): array {
        $headers = [
            'X-API-Key: ' . $this->apiKey,
            'Content-Type: application/json',
            'Accept: application/json',
        ];
        if ($idempotencyKey) $headers[] = 'Idempotency-Key: ' . $idempotencyKey;

        $ch = curl_init($this->baseUrl . $path);
        curl_setopt_array($ch, [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => $this->timeoutSeconds,
            CURLOPT_HTTPHEADER => $headers,
        ]);
        if ($body !== null) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR));
        }

        $raw = curl_exec($ch);
        if ($raw === false) {
            $message = curl_error($ch);
            curl_close($ch);
            throw new RuntimeException($message);
        }
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        $data = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
        if ($status < 200 || $status >= 300) {
            throw new RuntimeException($data['error']['message'] ?? $data['detail'] ?? 'BluePay request failed', $status);
        }
        return $data;
    }

    public function createInvoice(
        int $amountToman,
        string $orderId,
        ?string $description = null,
        string $feeMode = 'default',
        ?string $callbackUrl = null,
        ?int $ttlMinutes = null,
        ?string $idempotencyKey = null,
    ): array {
        $body = [
            'amount_toman' => $amountToman,
            'order_id' => $orderId,
            'description' => $description,
            'fee_mode' => $feeMode,
        ];
        if ($callbackUrl !== null) $body['callback_url'] = $callbackUrl;
        if ($ttlMinutes !== null) $body['ttl_minutes'] = $ttlMinutes;
        return $this->request('/api/v1/invoices', 'POST', $body, $idempotencyKey ?? 'invoice-' . $orderId);
    }

    public function getInvoice(string $paymentId): array
    {
        return $this->request('/api/v1/invoices/' . rawurlencode($paymentId));
    }

    public function getInvoiceTimeline(string $paymentId): array
    {
        return $this->request('/api/v1/invoices/' . rawurlencode($paymentId) . '/timeline');
    }

    public function cancelInvoice(string $paymentId, ?string $idempotencyKey = null): array
    {
        return $this->request(
            '/api/v1/invoices/' . rawurlencode($paymentId) . '/cancel',
            'POST',
            null,
            $idempotencyKey ?? 'cancel-' . $paymentId,
        );
    }

    public function createSandboxInvoice(
        int $amountToman,
        string $orderId,
        ?string $description = null,
        ?string $idempotencyKey = null,
    ): array {
        return $this->request(
            '/api/v1/sandbox/invoices',
            'POST',
            ['amount_toman' => $amountToman, 'order_id' => $orderId, 'description' => $description],
            $idempotencyKey ?? 'sandbox-' . $orderId,
        );
    }

    public function getSandboxInvoice(string $paymentId): array
    {
        return $this->request('/api/v1/sandbox/invoices/' . rawurlencode($paymentId));
    }

    public function simulateSandbox(string $paymentId, string $result = 'paid', ?string $referenceNumber = null): array
    {
        $body = ['result' => $result];
        if ($result === 'paid' && $referenceNumber !== null) $body['reference_number'] = $referenceNumber;
        return $this->request(
            '/api/v1/sandbox/invoices/' . rawurlencode($paymentId) . '/simulate',
            'POST',
            $body,
            'sandbox-simulate-' . $paymentId . '-' . $result,
        );
    }
}
