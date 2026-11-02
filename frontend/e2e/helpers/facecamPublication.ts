export const publicationCandidates = {
  title_candidates: Array.from({length: 8}, (_, i) => `Ce ninja cache un secret ${i + 1}`),
  description: 'Une histoire inattendue.', youtube_tags: ['anime'],
};
export const publicationMetadata = (title = publicationCandidates.title_candidates[0]) => ({
  facebook: {title, description: `${publicationCandidates.description}\n\n#anime #animerecommendations #manga #animefr #otaku`, tags: ['Anime TV & Movies']},
  youtube: {title, description: publicationCandidates.description, tags: publicationCandidates.youtube_tags},
  instagram: {caption: `${publicationCandidates.description}\n\n#anime #animefr #animerecommendations`}, tiktok: {description: 'Recommendation anime #animerecommendations'},
});
export const readyMetadata = (revision: number) => ({revision, generation_id: 'generation-1', candidates: publicationCandidates,
  metadata: publicationMetadata(), ready: true});
export const thumbnailOptions = Array.from({length: 10}, (_, i) => ({id: `image-${i}`, episode: 'episode-1', label: 'Episode 1', timestamp: i * 10 + 20, crop_x: .5, crop_y: .5}));
export const readyThumbnails = () => ({version: 'sources-1', candidates: thumbnailOptions, selected: thumbnailOptions[0], ready: true,
  generated: true, episodes: [{id: 'episode-1', label: 'Episode 1', duration: 1400, fps: 24}], warning: null});
export const thumbnailImage = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#202a60"/><circle cx="300" cy="150" r="100" fill="#b8cdf5"/><path d="M100 360L300 160L500 360Z" fill="#708abe"/></svg>';
