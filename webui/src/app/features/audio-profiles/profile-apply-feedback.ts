import { ProfileApplyOutcome } from './profile-api';

export function profileApplyFeedback(outcome: Partial<ProfileApplyOutcome>, saved: string) {
  const restarted = outcome.restarted_source_ids ?? [];
  const pending = Object.keys(outcome.restart_pending ?? {});
  const errors = Object.entries(outcome.restart_errors ?? {});
  const messages = [saved];
  if (restarted.length) messages.push(`Audio restarted for Source IDs ${restarted.join(', ')}.`);
  if (pending.length) messages.push(`Audio application pending for Source IDs ${pending.join(', ')}.`);
  return {
    message: messages.join(' '),
    error: errors.length
      ? 'Audio restart failed. ' + errors.map(([id, error]) => `Source ${id}: ${error}`).join('; ')
      : '',
  };
}
