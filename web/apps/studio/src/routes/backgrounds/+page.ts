import { redirect } from '@sveltejs/kit';

export const trailingSlash = 'always';
export function load() {
  redirect(307, '/?tab=settings');
}
