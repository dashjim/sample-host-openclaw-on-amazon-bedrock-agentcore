import { config } from '../config';
import { getToken } from './auth';

async function apiClient(
  path: string,
  options: RequestInit = {}
): Promise<Response> {
  const token = await getToken();
  if (!token) {
    window.location.href = '/login';
    throw new Error('Not authenticated');
  }

  const url = `${config.apiUrl}${path}`;
  const resp = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...options.headers,
    },
  });

  if (resp.status === 401) {
    window.location.href = '/login';
    throw new Error('Unauthorized');
  }

  return resp;
}

export async function get<T = unknown>(path: string): Promise<T> {
  const resp = await apiClient(path);
  return resp.json();
}

export async function post<T = unknown>(
  path: string,
  body: unknown
): Promise<T> {
  const resp = await apiClient(path, {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function put<T = unknown>(
  path: string,
  body: unknown
): Promise<T> {
  const resp = await apiClient(path, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function del<T = unknown>(path: string): Promise<T> {
  const resp = await apiClient(path, { method: 'DELETE' });
  return resp.json();
}
