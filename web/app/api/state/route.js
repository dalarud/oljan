// Server-side proxy for the engine's state.json (avoids any CORS concerns and
// lets us swap the source via the STATE_URL env var on Vercel).
export const dynamic = "force-dynamic";
export const revalidate = 0;

const DEFAULT_URL =
  "https://raw.githubusercontent.com/dalarud/oljan/state/state.json";

export async function GET() {
  const url = process.env.STATE_URL || DEFAULT_URL;
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) {
      return Response.json(
        { error: `upstream ${r.status}`, updated_at: null },
        { status: 200 }
      );
    }
    const data = await r.json();
    return Response.json(data, { status: 200 });
  } catch (e) {
    return Response.json({ error: String(e), updated_at: null }, { status: 200 });
  }
}
