// Return targets stay within the warehouse router, including on direct links.
export function datasetListReturn(state: unknown): string {
  const value = (state as { datasetsReturnTo?: unknown } | null)?.datasetsReturnTo;
  return typeof value === 'string' && /^\/datasets(?:\?[^#\\]*)?$/.test(value) ? value : '/datasets';
}

export function datasetDirectoryReturn(state: unknown, datasetId: string): string {
  const root = `/datasets/${encodeURIComponent(datasetId)}`;
  const value = (state as { directoryReturnTo?: unknown } | null)?.directoryReturnTo;
  return typeof value === 'string' && (value === root || value.startsWith(root + '?')) && !/[#\\]/.test(value) ? value : root;
}
