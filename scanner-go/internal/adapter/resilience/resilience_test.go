package resilience

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"nafaz_arb/internal/domain"
)

func TestRateLimiter(t *testing.T) {
	rl := NewRateLimiter(20)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	for i := 0; i < 25; i++ {
		if err := rl.Wait(ctx); err != nil {
			t.Fatalf("unexpected wait error at %d: %v", i, err)
		}
	}
}

func TestCircuitBreakerTransitions(t *testing.T) {
	cb := NewCircuitBreaker(3, 40*time.Millisecond)
	if cb.State() != StateClosed {
		t.Fatalf("expected CLOSED, got %s", cb.State())
	}

	cb.RecordFailure()
	cb.RecordFailure()
	if cb.State() != StateClosed {
		t.Fatalf("expected CLOSED after 2 failures, got %s", cb.State())
	}

	cb.RecordFailure()
	if cb.State() != StateOpen {
		t.Fatalf("expected OPEN after 3 failures, got %s", cb.State())
	}
	if err := cb.Allow(); !errors.Is(err, ErrCircuitOpen) {
		t.Fatalf("expected ErrCircuitOpen, got %v", err)
	}

	time.Sleep(50 * time.Millisecond)
	if cb.State() != StateHalfOpen {
		t.Fatalf("expected HALF_OPEN after cooldown, got %s", cb.State())
	}
	if err := cb.Allow(); err != nil {
		t.Fatalf("expected nil on HALF_OPEN allow, got %v", err)
	}

	cb.RecordSuccess()
	if cb.State() != StateClosed {
		t.Fatalf("expected CLOSED after success, got %s", cb.State())
	}
}

func TestClientRetryOn429(t *testing.T) {
	var attempts atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := attempts.Add(1)
		if n <= 2 {
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"msg":"rate limit"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	client := NewClient(ClientConfig{
		RateLimitRPS:            100,
		MaxRetries:              3,
		BaseBackoff:             5 * time.Millisecond,
		CircuitBreakerThreshold: 5,
		CircuitBreakerCooldown:  time.Second,
	})

	body, code, err := client.GetJSON(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("expected success after retry, got err: %v", err)
	}
	if code != http.StatusOK {
		t.Fatalf("expected 200, got %d", code)
	}
	if string(body) != `{"ok":true}` {
		t.Fatalf("unexpected body: %s", string(body))
	}
	if client.HTTP429Count() != 2 {
		t.Fatalf("expected 2 HTTP 429 recorded, got %d", client.HTTP429Count())
	}
}

func TestPollingInterval(t *testing.T) {
	cfg := domain.PollingConfig{
		FundingIntervalSec:     30,
		FundingFastIntervalSec: 5,
	}
	now := time.UnixMilli(1_700_000_000_000)

	// 10 minutes before settlement -> normal interval (30s)
	d1 := PollingInterval(now, now.Add(10*time.Minute).UnixMilli(), cfg)
	if d1 != 30*time.Second {
		t.Fatalf("expected 30s, got %v", d1)
	}

	// 90 seconds before settlement -> fast interval (5s)
	d2 := PollingInterval(now, now.Add(90*time.Second).UnixMilli(), cfg)
	if d2 != 5*time.Second {
		t.Fatalf("expected 5s, got %v", d2)
	}
}
