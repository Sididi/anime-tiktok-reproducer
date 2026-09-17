import {test, expect} from '@playwright/test';
import {readSSEStream} from '../src/utils/sse';

test('SSE preflight errors preserve actionable backend details', async () => {
  await expect(readSSEStream(Response.json({detail:'Préparez à nouveau l’aperçu audio.'},{status:400}),()=>{}))
    .rejects.toThrow('Préparez à nouveau l’aperçu audio.');
  await expect(readSSEStream(Response.json({detail:[{msg:'Révision invalide'}]},{status:422}),()=>{}))
    .rejects.toThrow('Révision invalide');
});

test('SSE errors fall back to status for non-JSON responses', async () => {
  await expect(readSSEStream(new Response('<html>Proxy error</html>',{status:502}),()=>{}))
    .rejects.toThrow('SSE request failed with status 502');
});

test('successful SSE streams retain their events', async () => {
  const events: unknown[]=[];
  const last=await readSSEStream(new Response('data: {"status":"processing"}\n\ndata: {"status":"complete"}\n\n'), event=>events.push(event));
  expect(events).toEqual([{status:'processing'},{status:'complete'}]);
  expect(last).toEqual({status:'complete'});
});
