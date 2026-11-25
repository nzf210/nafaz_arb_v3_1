package sink

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"nafaz_arb/internal/domain"
)

var ErrKeyNotFound = errors.New("key not found")

type KVStore interface {
	Get(ctx context.Context, key string) (string, error)
	SetTTL(ctx context.Context, key, value string, ttl time.Duration) error
	Del(ctx context.Context, key string) error
	Publish(ctx context.Context, channel, payload string) error
	HSet(ctx context.Context, key string, fields map[string]string) error
	HIncrBy(ctx context.Context, key, field string, delta int64) (int64, error)
}

type memEntry struct {
	value     string
	expiresAt time.Time
}

type MemoryKVStore struct {
	mu        sync.RWMutex
	nowFunc   func() time.Time
	kv        map[string]memEntry
	hashes    map[string]map[string]string
	published map[string][]string
}

func NewMemoryKVStore() *MemoryKVStore {
	return &MemoryKVStore{
		nowFunc:   time.Now,
		kv:        make(map[string]memEntry),
		hashes:    make(map[string]map[string]string),
		published: make(map[string][]string),
	}
}

func (m *MemoryKVStore) SetClock(nowFunc func() time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.nowFunc = nowFunc
}

func (m *MemoryKVStore) Get(_ context.Context, key string) (string, error) {
	m.mu.RLock()
	entry, ok := m.kv[key]
	now := m.nowFunc()
	m.mu.RUnlock()
	if !ok {
		return "", ErrKeyNotFound
	}
	if !entry.expiresAt.IsZero() && !now.Before(entry.expiresAt) {
		m.mu.Lock()
		delete(m.kv, key)
		m.mu.Unlock()
		return "", ErrKeyNotFound
	}
	return entry.value, nil
}

func (m *MemoryKVStore) SetTTL(_ context.Context, key, value string, ttl time.Duration) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	var exp time.Time
	if ttl > 0 {
		exp = m.nowFunc().Add(ttl)
	}
	m.kv[key] = memEntry{value: value, expiresAt: exp}
	return nil
}

func (m *MemoryKVStore) Del(_ context.Context, key string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.kv, key)
	delete(m.hashes, key)
	return nil
}

func (m *MemoryKVStore) Publish(_ context.Context, channel, payload string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.published[channel] = append(m.published[channel], payload)
	return nil
}

func (m *MemoryKVStore) HSet(_ context.Context, key string, fields map[string]string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.hashes[key]; !ok {
		m.hashes[key] = make(map[string]string)
	}
	for k, v := range fields {
		m.hashes[key][k] = v
	}
	return nil
}

func (m *MemoryKVStore) HIncrBy(_ context.Context, key, field string, delta int64) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.hashes[key]; !ok {
		m.hashes[key] = make(map[string]string)
	}
	var curr int64
	if raw, exists := m.hashes[key][field]; exists {
		parsed, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			return 0, err
		}
		curr = parsed
	}
	next := curr + delta
	m.hashes[key][field] = strconv.FormatInt(next, 10)
	return next, nil
}

func (m *MemoryKVStore) PublishedMessages(channel string) []string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	msgs := m.published[channel]
	out := make([]string, len(msgs))
	copy(out, msgs)
	return out
}

func (m *MemoryKVStore) GetHash(key string) map[string]string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	h, ok := m.hashes[key]
	if !ok {
		return nil
	}
	out := make(map[string]string, len(h))
	for k, v := range h {
		out[k] = v
	}
	return out
}

type RESPClient struct {
	addr    string
	timeout time.Duration
}

func NewRESPClient(addr string, timeout time.Duration) *RESPClient {
	if timeout <= 0 {
		timeout = 3 * time.Second
	}
	return &RESPClient{addr: addr, timeout: timeout}
}

func (c *RESPClient) execCommand(ctx context.Context, args ...string) (string, int64, bool, error) {
	d := net.Dialer{Timeout: c.timeout}
	conn, err := d.DialContext(ctx, "tcp", c.addr)
	if err != nil {
		return "", 0, false, err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(c.timeout))

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("*%d\r\n", len(args)))
	for _, arg := range args {
		sb.WriteString(fmt.Sprintf("$%d\r\n%s\r\n", len(arg), arg))
	}
	if _, err := io.WriteString(conn, sb.String()); err != nil {
		return "", 0, false, err
	}

	r := bufio.NewReader(conn)
	line, err := r.ReadString('\n')
	if err != nil {
		return "", 0, false, err
	}
	line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
	if len(line) == 0 {
		return "", 0, false, errors.New("empty RESP response")
	}

	switch line[0] {
	case '+':
		return line[1:], 0, false, nil
	case '-':
		return "", 0, false, errors.New(line[1:])
	case ':':
		n, err := strconv.ParseInt(line[1:], 10, 64)
		return "", n, false, err
	case '$':
		size, err := strconv.Atoi(line[1:])
		if err != nil {
			return "", 0, false, err
		}
		if size < 0 {
			return "", 0, true, nil
		}
		buf := make([]byte, size+2)
		if _, err := io.ReadFull(r, buf); err != nil {
			return "", 0, false, err
		}
		return string(buf[:size]), 0, false, nil
	default:
		return "", 0, false, fmt.Errorf("unexpected RESP prefix: %q", line)
	}
}

func (c *RESPClient) Get(ctx context.Context, key string) (string, error) {
	str, _, isNil, err := c.execCommand(ctx, "GET", key)
	if err != nil {
		return "", err
	}
	if isNil {
		return "", ErrKeyNotFound
	}
	return str, nil
}

func (c *RESPClient) SetTTL(ctx context.Context, key, value string, ttl time.Duration) error {
	ms := ttl.Milliseconds()
	if ms <= 0 {
		_, _, _, err := c.execCommand(ctx, "SET", key, value)
		return err
	}
	_, _, _, err := c.execCommand(ctx, "SET", key, value, "PX", strconv.FormatInt(ms, 10))
	return err
}

func (c *RESPClient) Del(ctx context.Context, key string) error {
	_, _, _, err := c.execCommand(ctx, "DEL", key)
	return err
}

func (c *RESPClient) Publish(ctx context.Context, channel, payload string) error {
	_, _, _, err := c.execCommand(ctx, "PUBLISH", channel, payload)
	return err
}

func (c *RESPClient) HSet(ctx context.Context, key string, fields map[string]string) error {
	if len(fields) == 0 {
		return nil
	}
	args := make([]string, 0, 2+len(fields)*2)
	args = append(args, "HSET", key)
	for k, v := range fields {
		args = append(args, k, v)
	}
	_, _, _, err := c.execCommand(ctx, args...)
	return err
}

func (c *RESPClient) HIncrBy(ctx context.Context, key, field string, delta int64) (int64, error) {
	_, val, _, err := c.execCommand(ctx, "HINCRBY", key, field, strconv.FormatInt(delta, 10))
	return val, err
}

type SignalPublisher struct {
	store            KVStore
	dedupCooldown    time.Duration
	minYieldDelta    float64
	dryRun           bool
	publishedCountMu sync.Mutex
	publishedCount   int64
	droppedCount     int64
	needsReset       bool
}

func NewSignalPublisher(store KVStore, cfg domain.EngineConfig) *SignalPublisher {
	cooldown := time.Duration(cfg.DedupCooldownSec) * time.Second
	if cooldown <= 0 {
		cooldown = 300 * time.Second
	}
	return &SignalPublisher{
		store:         store,
		dedupCooldown: cooldown,
		minYieldDelta: cfg.MinYieldDelta,
		dryRun:        cfg.DryRun,
	}
}

func (p *SignalPublisher) PublishSignal(ctx context.Context, sig domain.SignalPayload) (bool, error) {
	if err := sig.Validate(); err != nil {
		return false, err
	}

	redisDedupKey := domain.KeyPrefixCoreDedup + sig.DedupKey
	existingVal, err := p.store.Get(ctx, redisDedupKey)
	if err != nil && !errors.Is(err, ErrKeyNotFound) {
		return false, err
	}

	if err == nil {
		oldYield, parseErr := strconv.ParseFloat(existingVal, 64)
		if parseErr == nil {
			if sig.Metrics.NetDailyYield-oldYield < p.minYieldDelta {
				p.publishedCountMu.Lock()
				p.droppedCount++
				p.publishedCountMu.Unlock()
				return false, nil
			}
		}
	}

	yieldStr := strconv.FormatFloat(sig.Metrics.NetDailyYield, 'f', 8, 64)
	if err := p.store.SetTTL(ctx, redisDedupKey, yieldStr, p.dedupCooldown); err != nil {
		return false, err
	}

	rawJSON, err := json.Marshal(sig)
	if err != nil {
		return false, err
	}

	if err := p.store.Publish(ctx, domain.KeyCoreSignals, string(rawJSON)); err != nil {
		return false, err
	}

	p.publishedCountMu.Lock()
	p.publishedCount++
	p.publishedCountMu.Unlock()
	return true, nil
}

func (p *SignalPublisher) PublishBatch(ctx context.Context, signals []domain.SignalPayload) (int, error) {
	published := 0
	for _, sig := range signals {
		ok, err := p.PublishSignal(ctx, sig)
		if err != nil {
			return published, err
		}
		if ok {
			published++
		}
	}
	return published, nil
}

func (p *SignalPublisher) UpdateExchangeHealth(ctx context.Context, cex, status string, lastPingMs int64, activeSymbols, http429Count int) error {
	key := domain.KeyPrefixMetaExchange + cex
	fields := map[string]string{
		"status":               status,
		"last_ping_ms":         strconv.FormatInt(lastPingMs, 10),
		"active_symbols_count": strconv.Itoa(activeSymbols),
		"http_429_count":       strconv.Itoa(http429Count),
	}
	return p.store.HSet(ctx, key, fields)
}

func (p *SignalPublisher) UpdateCollateralSnapshot(ctx context.Context, snap *domain.CollateralSnapshot) error {
	if snap == nil {
		return errors.New("collateral snapshot is nil")
	}
	key := domain.KeyPrefixMetaCollateral + snap.Exchange
	fields := make(map[string]string, len(snap.AssetRatios)+1)
	fields["timestamp_ms"] = strconv.FormatInt(snap.TimestampMs, 10)
	for asset, ratio := range snap.AssetRatios {
		fields[asset] = strconv.FormatFloat(ratio, 'f', 4, 64)
	}
	return p.store.HSet(ctx, key, fields)
}

func (p *SignalPublisher) ResetRejectionStats(ctx context.Context) error {
	p.publishedCountMu.Lock()
	p.needsReset = true
	p.publishedCountMu.Unlock()
	if err := p.store.Del(ctx, domain.KeyMetaRejectionStats); err != nil {
		return err
	}
	p.publishedCountMu.Lock()
	p.needsReset = false
	p.publishedCountMu.Unlock()
	return nil
}

func (p *SignalPublisher) RecordRejectionStats(ctx context.Context, stats map[string]int64) error {
	p.publishedCountMu.Lock()
	needsReset := p.needsReset
	p.publishedCountMu.Unlock()
	if needsReset {
		if err := p.store.Del(ctx, domain.KeyMetaRejectionStats); err != nil {
			return err
		}
		p.publishedCountMu.Lock()
		p.needsReset = false
		p.publishedCountMu.Unlock()
	}
	for reason, count := range stats {
		if count <= 0 {
			continue
		}
		if _, err := p.store.HIncrBy(ctx, domain.KeyMetaRejectionStats, reason, count); err != nil {
			return err
		}
	}
	return nil
}

func (p *SignalPublisher) Stats() (published int64, dropped int64) {
	p.publishedCountMu.Lock()
	defer p.publishedCountMu.Unlock()
	return p.publishedCount, p.droppedCount
}
