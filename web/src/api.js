async function request(path) {
  const response = await fetch(path, { credentials: 'same-origin' })
  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok) {
    throw new Error(data?.error || `${response.status} ${response.statusText}`)
  }
  return data
}

export const api = {
  wishlist: () => request('/api/wishlist'),
  chart: (appId, range = '1m') => request(`/api/wishlist/${appId}/chart?range=${encodeURIComponent(range)}`),
  history: (appId, page = 1) => request(`/api/wishlist/${appId}/history?page=${page}&per_page=8`),
  countries: (appId, range = '1m') => request(`/api/wishlist/${appId}/countries?range=${encodeURIComponent(range)}`)
}
