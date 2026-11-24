export default async function handler(req, res) {
  const url = new URL(req.url, 'http://localhost');
  const targetUrl = `https://api.telegram.org${url.pathname}${url.search}`;

  const headers = {};
  for (const [key, value] of Object.entries(req.headers)) {
    if (key.toLowerCase() !== 'host') {
      headers[key] = value;
    }
  }

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers: headers,
      body: req.method !== 'GET' && req.method !== 'HEAD' ? (typeof req.body === 'string' ? req.body : JSON.stringify(req.body)) : undefined,
    });

    res.status(upstream.status);
    upstream.headers.forEach((val, key) => {
      res.setHeader(key, val);
    });

    const buf = await upstream.arrayBuffer();
    return res.send(Buffer.from(buf));
  } catch (err) {
    return res.status(502).json({ error: String(err) });
  }
}

