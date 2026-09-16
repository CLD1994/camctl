import { useEffect, useState } from "react";

/** 固定发起操作的页面，异步完成后仍使用原来的归属。 */
export function useFeedback(
  scope: string,
  lifetime?: number,
): [string, (text: string) => void] {
  const [message, setMessage] = useState<{ scope: string; text: string }>();
  useEffect(() => {
    if (!message || lifetime === undefined) return;
    const timer = setTimeout(
      () =>
        setMessage((current) => (current === message ? undefined : current)),
      lifetime,
    );
    return () => clearTimeout(timer);
  }, [message, lifetime]);
  return [
    message?.scope === scope ? message.text : "",
    (text) => setMessage(text ? { scope, text } : undefined),
  ];
}
