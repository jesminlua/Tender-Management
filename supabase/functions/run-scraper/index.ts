// supabase/functions/run-scraper/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    let body: { site_id?: string } = {};
    try { body = await req.json(); } catch { /* no body is fine */ }

    const authHeader = req.headers.get("Authorization");
    if (!authHeader) {
      return new Response(
        JSON.stringify({ error: "Unauthorised" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const { data: { user }, error: authErr } = await supabase.auth.getUser(
      authHeader.replace("Bearer ", ""),
    );
    if (authErr || !user) {
      return new Response(
        JSON.stringify({ error: "Invalid token" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    let query = supabase.from("sites").select("id, name").eq("enabled", true);
    if (body.site_id) query = query.eq("id", body.site_id);
    const { data: sites, error: sitesErr } = await query;
    if (sitesErr) throw sitesErr;
    if (!sites || sites.length === 0) {
      return new Response(
        JSON.stringify({ message: "No active sites configured." }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const jobs = sites.map((s: { id: string; name: string }) => ({
      site_id:      s.id,
      triggered_by: user.id,
      status:       "pending",
      created_at:   new Date().toISOString(),
    }));

    const { data: inserted, error: insertErr } = await supabase
      .from("scrape_queue")
      .insert(jobs)
      .select();

    if (insertErr) throw insertErr;

    return new Response(
      JSON.stringify({
        message: `Queued ${inserted.length} scrape job(s).`,
        jobs:    inserted.map((j: { id: string; site_id: string }) => ({
          job_id:  j.id,
          site_id: j.site_id,
        })),
      }),
      { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );

  } catch (err) {
    console.error("run-scraper error:", err);
    return new Response(
      JSON.stringify({ error: String(err) }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
