import http from 'node:http';
import net from 'node:net';
import type { AddressInfo } from 'node:net';

export interface FakeMessage {
  id: string;
  raw: Buffer;
  receivedAt: string;
}

interface Reply {
  status?: number;
  body?: unknown;
  raw?: Buffer;
}

type Route = (req: {
  method: string;
  url: URL;
  form: URLSearchParams;
  token: string | null;
}) => Reply | null;

/** Servidor HTTP local con rutas simuladas; anota cada petición. */
async function startHttp(route: Route) {
  const requests: string[] = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    requests.push(`${req.method} ${decodeURIComponent(url.pathname + url.search)}`);
    const auth = req.headers.authorization;
    const reply = route({
      method: req.method ?? 'GET',
      url,
      form: new URLSearchParams(body),
      token: auth?.startsWith('Bearer ') ? auth.slice(7) : null,
    }) ?? { status: 404, body: { error: { message: `ruta desconocida ${url.pathname}` } } };
    if (reply.raw) {
      res.writeHead(reply.status ?? 200, { 'content-type': 'message/rfc822' }).end(reply.raw);
    } else {
      res
        .writeHead(reply.status ?? 200, { 'content-type': 'application/json' })
        .end(JSON.stringify(reply.body ?? {}));
    }
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return {
    url,
    requests,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

const unauthorized = { status: 401, body: { error: { message: 'Invalid Credentials' } } };

/**
 * Google simulado: endpoint de tokens y API de Gmail (perfil, búsqueda
 * paginada y mensajes en formato raw).
 */
export async function startFakeGmail({
  account = 'pcjhonny13@gmail.com',
  messages = [] as FakeMessage[],
  refreshToken = 'rt-gmail',
  failing = new Set<string>(),
} = {}) {
  const state = { messages, failing, queries: [] as string[] };
  const server = await startHttp(({ method, url, form, token }) => {
    if (method === 'POST' && url.pathname === '/token') {
      return form.get('refresh_token') === refreshToken
        ? { body: { access_token: 'gmail-at', expires_in: 3600 } }
        : {
            status: 400,
            body: {
              error: 'invalid_grant',
              error_description: 'Token has been expired or revoked.',
            },
          };
    }
    if (token !== 'gmail-at') return unauthorized;
    const base = '/gmail/v1/users/me';
    if (url.pathname === `${base}/profile`) return { body: { emailAddress: account } };
    if (url.pathname === `${base}/messages`) {
      state.queries.push(url.searchParams.get('q') ?? '');
      // Gmail: del más reciente al más antiguo, en páginas (aquí de 2).
      const all = [...state.messages].sort((a, b) => b.receivedAt.localeCompare(a.receivedAt));
      const start = Number(url.searchParams.get('pageToken') ?? 0);
      const page = all.slice(start, start + 2);
      return {
        body: {
          messages: page.map((m) => ({ id: m.id, threadId: m.id })),
          ...(start + 2 < all.length && { nextPageToken: String(start + 2) }),
        },
      };
    }
    const m = /^\/gmail\/v1\/users\/me\/messages\/([^/]+)$/.exec(url.pathname);
    if (m) {
      if (state.failing.has(m[1]))
        return { status: 500, body: { error: { message: 'Backend Error' } } };
      const message = state.messages.find((x) => x.id === m[1]);
      return message ? { body: { id: message.id, raw: message.raw.toString('base64url') } } : null;
    }
    return null;
  });
  return {
    ...server,
    state,
    endpoints: { gmailApiUrl: `${server.url}/gmail/v1`, googleTokenUrl: `${server.url}/token` },
  };
}

/**
 * Microsoft simulado: tokens (permiso de aplicación y refresh token con
 * rotación) y Graph (carpetas, mensajes paginados con nextLink y MIME).
 */
export async function startFakeGraph({
  mailbox = 'facturas@empresa.pe',
  me = 'gabicho_05@outlook.com',
  messages = [] as FakeMessage[],
  clientSecret = 'secreto',
  refreshToken = 'rt-ms-1',
} = {}) {
  const state = { messages, refreshToken, issued: 0, failing: new Set<string>() };
  const server = await startHttp(({ method, url, form, token }) => {
    if (method === 'POST' && url.pathname.endsWith('/oauth2/v2.0/token')) {
      const grant = form.get('grant_type');
      if (grant === 'client_credentials' && form.get('client_secret') === clientSecret) {
        return { body: { access_token: 'graph-at', expires_in: 3600 } };
      }
      if (grant === 'refresh_token' && form.get('refresh_token') === state.refreshToken) {
        state.refreshToken = `rt-ms-${++state.issued + 1}`;
        return {
          body: { access_token: 'graph-at', refresh_token: state.refreshToken, expires_in: 3600 },
        };
      }
      return {
        status: 400,
        body: { error: 'invalid_grant', error_description: 'AADSTS70000: invalid grant.' },
      };
    }
    if (token !== 'graph-at') return unauthorized;
    const path = decodeURIComponent(url.pathname).replace(/^\/v1\.0/, '');
    const roots = [`/users/${mailbox}`, '/me'];
    const root = roots.find((r) => path === r || path.startsWith(`${r}/`));
    if (!root) return { status: 404, body: { error: { message: 'ErrorInvalidUser' } } };
    const rest = path.slice(root.length);
    if (rest === '') return { body: { mail: me, userPrincipalName: me } };
    if (rest === '/mailFolders/inbox') return { body: { id: 'inbox-id' } };
    if (rest === '/mailFolders' || rest === '/mailFolders/inbox/childFolders') {
      const wanted = /displayName eq '(.*)'/.exec(url.searchParams.get('$filter') ?? '')?.[1];
      return {
        body: {
          value:
            rest.endsWith('childFolders') && wanted === 'Facturas' ? [{ id: 'facturas-id' }] : [],
        },
      };
    }
    const list = /^\/mailFolders\/([^/]+)\/messages$/.exec(rest);
    if (list) {
      const since =
        /receivedDateTime ge (\S+)/.exec(url.searchParams.get('$filter') ?? '')?.[1] ?? '';
      const all = state.messages.filter((m) => m.receivedAt >= since);
      const skip = Number(url.searchParams.get('$skiptoken') ?? 0);
      const next = new URL(url.toString().replace('http://127.0.0.1', server.url));
      next.searchParams.set('$skiptoken', String(skip + 2));
      return {
        body: {
          value: all
            .slice(skip, skip + 2)
            .map((m) => ({ id: m.id, receivedDateTime: m.receivedAt })),
          ...(skip + 2 < all.length && { '@odata.nextLink': next.toString() }),
        },
      };
    }
    const raw = /^\/messages\/([^/]+)\/\$value$/.exec(rest);
    if (raw) {
      if (state.failing.has(raw[1])) return { status: 503, body: { error: { message: 'busy' } } };
      const message = state.messages.find((m) => m.id === raw[1]);
      return message ? { raw: message.raw } : null;
    }
    return null;
  });
  return {
    ...server,
    state,
    endpoints: { graphApiUrl: `${server.url}/v1.0`, microsoftLoginUrl: server.url },
  };
}

/**
 * Servidor IMAP mínimo (sin TLS) con lo que usa el conector: LOGIN,
 * EXAMINE, UID SEARCH, UID FETCH (estructura y mensaje completo) y LOGOUT.
 */
export async function startFakeImap({
  user = 'facturas@empresa.pe',
  password = 'clave-app',
  messages = [] as (FakeMessage & { attachment: boolean })[],
} = {}) {
  const state = { commands: [] as string[], examined: false };
  const months = [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'May',
    'Jun',
    'Jul',
    'Aug',
    'Sep',
    'Oct',
    'Nov',
    'Dec',
  ];
  const internalDate = (iso: string) => {
    const d = new Date(iso);
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${pad(d.getUTCDate())}-${months[d.getUTCMonth()]}-${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} +0000`;
  };
  const structure = (attachment: boolean) =>
    attachment
      ? '(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 10 1 NIL NIL NIL NIL)' +
        '("APPLICATION" "PDF" ("NAME" "f.pdf") NIL NIL "BASE64" 100 NIL ("ATTACHMENT" ("FILENAME" "f.pdf")) NIL NIL)' +
        ' "MIXED" ("BOUNDARY" "x") NIL NIL NIL)'
      : '("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 10 1 NIL NIL NIL NIL)';
  const uidOf = (m: FakeMessage) => Number(m.id);

  const server = net.createServer((socket) => {
    socket.write('* OK [CAPABILITY IMAP4rev1 LITERAL+] Fake IMAP listo\r\n');
    let buffer = '';
    socket.on('data', (chunk) => {
      buffer += chunk.toString('latin1');
      let i: number;
      while ((i = buffer.indexOf('\r\n')) >= 0) {
        const line = buffer.slice(0, i);
        buffer = buffer.slice(i + 2);
        handle(line);
      }
    });
    socket.on('error', () => {});

    function handle(line: string) {
      const [tag, ...words] = line.split(' ');
      const command = words.join(' ');
      const upper = command.toUpperCase();
      state.commands.push(upper.startsWith('LOGIN') ? 'LOGIN ***' : command);
      const ok = (text = 'OK') => socket.write(`${tag} ${text}\r\n`);
      if (upper === 'CAPABILITY') {
        socket.write('* CAPABILITY IMAP4rev1 LITERAL+\r\n');
        return ok();
      }
      if (upper.startsWith('LOGIN')) {
        const [, u, p] = /^LOGIN "?([^" ]+)"? "?([^"]+)"?$/i.exec(command) ?? [];
        return u === user && p === password
          ? ok('OK [CAPABILITY IMAP4rev1 LITERAL+] Logged in')
          : ok('NO [AUTHENTICATIONFAILED] Invalid credentials');
      }
      if (upper.startsWith('EXAMINE') || upper.startsWith('SELECT')) {
        state.examined = true;
        socket.write(
          `* ${messages.length} EXISTS\r\n* 0 RECENT\r\n* OK [UIDVALIDITY 7] UIDs valid\r\n` +
            `* OK [UIDNEXT ${messages.length + 1}] Predicted next UID\r\n* FLAGS (\\Seen)\r\n`,
        );
        return ok('OK [READ-ONLY] EXAMINE completed');
      }
      if (upper.startsWith('UID SEARCH')) {
        socket.write(`* SEARCH ${messages.map(uidOf).join(' ')}\r\n`);
        return ok();
      }
      if (upper.startsWith('UID FETCH')) {
        const [, , range] = command.split(' ');
        const uids = new Set(range!.split(',').map(Number));
        messages.forEach((m, index) => {
          if (!uids.has(uidOf(m))) return;
          if (upper.includes('BODYSTRUCTURE')) {
            socket.write(
              `* ${index + 1} FETCH (UID ${uidOf(m)} INTERNALDATE "${internalDate(m.receivedAt)}" BODYSTRUCTURE ${structure(m.attachment)})\r\n`,
            );
          } else {
            socket.write(
              `* ${index + 1} FETCH (UID ${uidOf(m)} RFC822.SIZE ${m.raw.length} BODY[] {${m.raw.length}}\r\n`,
            );
            socket.write(m.raw);
            socket.write(')\r\n');
          }
        });
        return ok();
      }
      if (upper === 'LOGOUT') {
        socket.write('* BYE\r\n');
        ok();
        return socket.end();
      }
      return ok();
    }
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    host: '127.0.0.1',
    port: (server.address() as AddressInfo).port,
    state,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
