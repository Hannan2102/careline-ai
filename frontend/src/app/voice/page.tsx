import { redirect } from "next/navigation";

/** The call now lives on the home page; old links still work. */
export default function VoicePage() {
  redirect("/");
}
