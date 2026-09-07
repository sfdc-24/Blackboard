// Render one prepared job into the body for the existing Meta messages POST.
// This step does not send. Recipient, identity and reply ID come from the
// authenticated preparation step, never from the model's response text.
export default defineComponent({
  props: {
    job: { type: 'object', description: 'One job from the context preparation step' },
    modelText: { type: 'string', optional: true, description: 'Successful model text for this exact job, when callModel is true' },
  },
  async run() {
    const job = this.job;
    if (job?.callModel === false) {
      const response = job.response;
      if (!/^wamid\.[A-Za-z0-9+/=_-]{1,500}$/.test(job.wamid || '') ||
          response?.context?.message_id !== job.wamid || !/^\d{6,20}$/.test(response?.to || '') ||
          response?.type !== 'text' || typeof response.text?.body !== 'string' ||
          !response.text.body.startsWith('[STATUS | gateway]\n') || response.text.body.length > 4096) {
        throw new Error('invalid prepared gateway acknowledgement');
      }
      return { messaging_product: 'whatsapp', recipient_type: 'individual', to: response.to,
        context: { message_id: job.wamid }, type: 'text', text: { preview_url: false, body: response.text.body } };
    }
    const identity = job?.responseIdentity;
    if (job?.callModel !== true || !/^gateway\/(claude|gemini|chatgpt|meta)$/.test(identity?.instance || '') ||
        !/^\d{6,20}$/.test(identity?.to || '') || identity.replyTo !== job.wamid ||
        !/^wamid\.[A-Za-z0-9+/=_-]{1,500}$/.test(job.wamid || '')) {
      throw new Error('invalid prepared gateway job');
    }
    if (typeof this.modelText !== 'string' || !this.modelText.trim()) {
      throw new Error('no model response; do not send a blank reply');
    }
    const body = `[STATUS | ${identity.instance}]\n` + this.modelText.trim();
    if (body.length > 4096) throw new Error('response exceeds WhatsApp text limit');
    return { messaging_product: 'whatsapp', recipient_type: 'individual', to: identity.to,
      context: { message_id: job.wamid }, type: 'text', text: { preview_url: false, body } };
  },
});
