import { ActionUiIdentity } from "./action-ui-identity";
import type { DraftSession } from "./session";
const identities = new WeakMap<DraftSession, ActionUiIdentity>();
export function identityForSession(session: DraftSession) {
  let identity = identities.get(session);
  if (!identity) {
    identity = new ActionUiIdentity(session.content);
    identities.set(session, identity);
  }
  return identity;
}
export function bindSessionWholeView(session: DraftSession) {
  const identity = identityForSession(session);
  const releaseWhole = session.bindWholeView((input) => identity.prepareWhole(input));
  const releaseAppend = session.bindAppendView((input) => identity.prepareAppend(input));
  return () => { releaseAppend(); releaseWhole(); };
}
