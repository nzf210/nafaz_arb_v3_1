package resilience

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"nafaz_arb/internal/domain"
)

var (
	ErrCircuitOpen = errors.New("circuit breaker is open")
	ErrRateLimited = errors.New("rate limited")
)

type State string

const (
	StateClosed   State = "CLOSED"
	StateOpen     State = "OPEN"
	StateHalfOpen State = "HALF_OPEN"
)

// RateLimiter implements a simple token-bucket rate limiter using stdlib only.
// ponytail: single-process in-memory token bucket; upgrade to distributed/sliding window if multi-instance scanner shares IP quota.
type RateLimiter struct {
	mu         sync.Mutex
	rps        float64
	tokens     float64
	maxTokens  float64
	lastRefill time.Time
}

func NewRateLimiter(rps float64) *RateLimiter {
	if rps <= 0 {
		rps = 10
	}
	maxT := rps
	if maxT < 1 {
		maxT = 1
	}
	return &RateLimiter{
		rps:        rps,
		tokens:     maxT,
		maxTokens:  maxT,
		lastRefill: time.Now(),
	}
}

func (rl *RateLimiter) refillLocked(now time.Time) {
	elapsed := now.Sub(rl.lastRefill).Seconds()
	if elapsed > 0 {
		rl.tokens += elapsed * rl.rps
		if rl.tokens > rl.maxTokens {
			rl.tokens = rl.maxTokens
		}
		rl.lastRefill = now
	}
}

func (rl *RateLimiter) Allow() bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	rl.refillLocked(time.Now())
	if rl.tokens >= 1.0 {
		rl.tokens -= 1.0
		return true
	}
	return false
}

func (rl *RateLimiter) Wait(ctx context.Context) error {
	for {
		rl.mu.Lock()
		now := time.Now()
		rl.refillLocked(now)
		if rl.tokens >= 1.0 {
			rl.tokens -= 1.0
			rl.mu.Unlock()
			return nil
		}
		needed := 1.0 - rl.tokens
		waitDur := time.Duration((needed / rl.rps) * float64(time.Second))
		if waitDur < time.Millisecond {
			waitDur = time.Millisecond
		}
		rl.mu.Unlock()

		timer := time.NewTimer(waitDur)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}

type CircuitBreaker struct {
	mu               sync.Mutex
	state            State
	failureThreshold int
	failures         int
	cooldown         time.Duration
	openedAt         time.Time
}

func NewCircuitBreaker(threshold int, cooldown time.Duration) *CircuitBreaker {
	if threshold <= 0 {
		threshold = 5
	}
	if cooldown <= 0 {
		cooldown = 10 * time.Second
	}
	return &CircuitBreaker{
		state:            StateClosed,
		failureThreshold: threshold,
		cooldown:         cooldown,
	}
}

func (cb *CircuitBreaker) State() State {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	if cb.state == StateOpen && time.Since(cb.openedAt) >= cb.cooldown {
		cb.state = StateHalfOpen
	}
	return cb.state
}

func (cb *CircuitBreaker) Allow() error {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	if cb.state == StateOpen {
		if time.Since(cb.openedAt) >= cb.cooldown {
			cb.state = StateHalfOpen
			return nil
		}
		return ErrCircuitOpen
	}
	return nil
}

func (cb *CircuitBreaker) RecordSuccess() {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	cb.failures = 0
	cb.state = StateClosed
}

func (cb *CircuitBreaker) RecordFailure() {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	cb.failures++
	if cb.state == StateHalfOpen || cb.failures >= cb.failureThreshold {
		cb.state = StateOpen
		cb.openedAt = time.Now()
	}
}

type ClientConfig struct {
	RateLimitRPS            float64
	MaxRetries              int
	BaseBackoff             time.Duration
	CircuitBreakerThreshold int
	CircuitBreakerCooldown  time.Duration
	Timeout                 time.Duration
}

type Client struct {
	httpClient  *http.Client
	rateLimiter *RateLimiter
	breaker     *CircuitBreaker
	maxRetries  int
	baseBackoff time.Duration
	http429Cnt  atomic.Int64
}

func NewClient(cfg ClientConfig) *Client {
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	baseBackoff := cfg.BaseBackoff
	if baseBackoff <= 0 {
		baseBackoff = 25 * time.Millisecond
	}
	maxRetries := cfg.MaxRetries
	if maxRetries < 0 {
		maxRetries = 0
	}
	return &Client{
		httpClient:  &http.Client{Timeout: timeout},
		rateLimiter: NewRateLimiter(cfg.RateLimitRPS),
		breaker:     NewCircuitBreaker(cfg.CircuitBreakerThreshold, cfg.CircuitBreakerCooldown),
		maxRetries:  maxRetries,
		baseBackoff: baseBackoff,
	}
}

func NewClientFromExchangeConfig(cfg domain.ExchangeConfig) *Client {
	maxRetries := cfg.MaxRetries
	if maxRetries <= 0 {
		maxRetries = 3
	}
	return NewClient(ClientConfig{
		RateLimitRPS:            cfg.RateLimitRPS,
		MaxRetries:              maxRetries,
		BaseBackoff:             20 * time.Millisecond,
		CircuitBreakerThreshold: cfg.CircuitBreakerThreshold,
		CircuitBreakerCooldown:  10 * time.Second,
		Timeout:                 5 * time.Second,
	})
}

func (c *Client) HTTP429Count() int64 {
	return c.http429Cnt.Load()
}

func (c *Client) CircuitState() State {
	return c.breaker.State()
}

func (c *Client) GetJSON(ctx context.Context, url string) ([]byte, int, error) {
	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		if err := c.breaker.Allow(); err != nil {
			return nil, 0, err
		}
		if err := c.rateLimiter.Wait(ctx); err != nil {
			return nil, 0, err
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, 0, err
		}

		resp, err := c.httpClient.Do(req)
		if err != nil {
			c.breaker.RecordFailure()
			lastErr = err
			if attempt < c.maxRetries {
				if err := sleepWithBackoff(ctx, c.baseBackoff, attempt); err != nil {
					return nil, 0, err
				}
				continue
			}
			return nil, 0, lastErr
		}

		body, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		if readErr != nil {
			c.breaker.RecordFailure()
			lastErr = readErr
			if attempt < c.maxRetries {
				if err := sleepWithBackoff(ctx, c.baseBackoff, attempt); err != nil {
					return nil, resp.StatusCode, err
				}
				continue
			}
			return nil, resp.StatusCode, lastErr
		}

		if resp.StatusCode == http.StatusTooManyRequests {
			c.http429Cnt.Add(1)
			c.breaker.RecordFailure()
			lastErr = fmt.Errorf("%w: status 429", ErrRateLimited)
			if attempt < c.maxRetries {
				if err := sleepWithBackoff(ctx, c.baseBackoff, attempt); err != nil {
					return nil, resp.StatusCode, err
				}
				continue
			}
			return nil, resp.StatusCode, lastErr
		}

		if resp.StatusCode >= 500 {
			c.breaker.RecordFailure()
			lastErr = fmt.Errorf("upstream server error: status %d", resp.StatusCode)
			if attempt < c.maxRetries {
				if err := sleepWithBackoff(ctx, c.baseBackoff, attempt); err != nil {
					return nil, resp.StatusCode, err
				}
				continue
			}
			return nil, resp.StatusCode, lastErr
		}

		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return body, resp.StatusCode, fmt.Errorf("unexpected status %d", resp.StatusCode)
		}

		c.breaker.RecordSuccess()
		return body, resp.StatusCode, nil
	}
	return nil, 0, lastErr
}

func sleepWithBackoff(ctx context.Context, base time.Duration, attempt int) error {
	mult := 1 << attempt
	backoff := base * time.Duration(mult)
	jitter := time.Duration(rand.Int63n(int64(base) + 1))
	timer := time.NewTimer(backoff + jitter)
	select {
	case <-ctx.Done():
		timer.Stop()
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// PollingInterval implements NFR-1 dynamic funding rate polling schedule:
// accelerates to FundingFastIntervalSec within 2 minutes before nextSettlementMs.
func PollingInterval(now time.Time, nextSettlementMs int64, cfg domain.PollingConfig) time.Duration {
	normalSec := cfg.FundingIntervalSec
	if normalSec <= 0 {
		normalSec = 30
	}
	fastSec := cfg.FundingFastIntervalSec
	if fastSec <= 0 {
		fastSec = 5
	}
	if nextSettlementMs > 0 {
		rem := time.Until(time.UnixMilli(nextSettlementMs))
		if nextSettlementMs > now.UnixMilli() {
			rem = time.Duration(nextSettlementMs-now.UnixMilli()) * time.Millisecond
		} else {
			rem = 0
		}
		if rem > 0 && rem <= 2*time.Minute {
			return time.Duration(fastSec) * time.Second
		}
	}
	return time.Duration(normalSec) * time.Second
}
