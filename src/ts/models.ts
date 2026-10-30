/** Shared records. Calendar dates use ISO YYYY-MM-DD strings. */
export type FieldName = 'container_no' | 'container_size' | 'customs_seal';
export interface LayoutSpan { text: string; bbox: [number, number, number, number] }
export interface LayoutPage { number: number; width: number; height: number; spans: LayoutSpan[] }
