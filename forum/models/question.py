from base import * #todo maybe remove *
from tag import Tag
#todo: make uniform import for consts
from forum.const import CONST
from forum import const
from forum.utils.html import sanitize_html
from markdown2 import Markdown
from django.utils.html import strip_tags
import datetime
from django.conf import settings
from django.utils.datastructures import SortedDict
from forum.models.tag import MarkedTag
from django.db.models import Q

markdowner = Markdown(html4tags=True)

from forum.utils.lists import LazyList

#todo: too bad keys are duplicated see const sort methods
QUESTION_ORDER_BY_MAP = {
    'latest': '-added_at',
    'oldest': 'added_at',
    'active': '-last_activity_at',
    'inactive': 'last_activity_at',
    'hottest': '-answer_count',
    'coldest': 'answer_count',
    'mostvoted': '-score',
    'leastvoted': 'score',
    'relevant': None #this is a special case
}

class QuestionManager(models.Manager):
    def create_new(self, title=None,author=None,added_at=None, wiki=False,tagnames=None, text=None):
        html = sanitize_html(markdowner.convert(text))
        summary = strip_tags(html)[:120]
        question = Question(
            title = title,
            author = author,
            added_at = added_at,
            last_activity_at = added_at,
            last_activity_by = author,
            wiki = wiki,
            tagnames = tagnames,
            html = html,
            text = text,
            summary = summary
        )
        if question.wiki:
            question.last_edited_by = question.author
            question.last_edited_at = added_at
            question.wikified_at = added_at

        question.save()

        question.add_revision(
            author=author,
            text=text,
            comment=CONST['default_version'],
            revised_at=added_at,
        )
        return question

    def run_advanced_search(
                            self,
                            request_user = None,
                            scope_selector = const.DEFAULT_POST_SCOPE,#unanswered/all/favorite (for logged in)
                            search_query = None,
                            tag_selector = None,
                            author_selector = None,#???question or answer author or just contributor
                            sort_method = const.DEFAULT_POST_SORT_METHOD
                            ):
        """all parameters are guaranteed to be clean
        however may not relate to database - in that case
        a relvant filter will be silently dropped
        """

        qs = self.filter(deleted=False)#todo - add a possibility to see deleted questions

        #return metadata
        meta_data = {}
        if tag_selector: 
            for tag in tag_selector:
                qs = qs.filter(tags__name = tag)

        if search_query:
            try:
                qs = qs.filter( Q(title__search = search_query) \
                                | Q(text__search = search_query) \
                                | Q(tagnames__search = search_query) \
                                | Q(answers__text__search = search_query)
                                )
            except:
                #fallback to dumb title match search
                qs = qs.extra(
                                where=['title like %s'], 
                                params=['%' + search_query + '%']
                            )

        if scope_selector:
            if scope_selector == 'unanswered':
                if const.UNANSWERED_MEANING == 'NO_ANSWERS':
                    qs = qs.filter(answer_count=0)#todo: expand for different meanings of this
                elif const.UNANSWERED_MEANING == 'NO_ACCEPTED_ANSWERS':
                    qs = qs.filter(answer_accepted=False)
                elif const.UNANSWERED_MEANING == 'NO_UPVOTED_ANSWERS':
                    raise NotImplementedError()
                else:
                    raise Exception('UNANSWERED_MEANING setting is wrong')
            elif scope_selector == 'favorite':
                qs = qs.filter(favorited_by = request_user)
            
        #user contributed questions & answers
        if author_selector:
            try:
                u = User.objects.get(id=int(author_selector))
                qs = qs.filter(Q(author=u, deleted=False) | Q(answers__author=u, answers__deleted=False))
                meta_data['author_name'] = u.username
            except User.DoesNotExist:
                meta_data['author_name'] = None

        #get users tag filters
        if request_user and request_user.is_authenticated():
            uid_str = str(request_user.id)
            #mark questions tagged with interesting tags
            qs = qs.extra(
                            select = SortedDict([
                                (
                                    'interesting_score', 
                                    'SELECT COUNT(1) FROM forum_markedtag, question_tags '
                                      + 'WHERE forum_markedtag.user_id = %s '
                                      + 'AND forum_markedtag.tag_id = question_tags.tag_id '
                                      + 'AND forum_markedtag.reason = \'good\' '
                                      + 'AND question_tags.question_id = question.id'
                                ),
                                    ]),
                            select_params = (uid_str,),
                         )
            if request_user.hide_ignored_questions:
                #exclude ignored tags if the user wants to
                ignored_tags = Tag.objects.filter(user_selections__reason='bad',
                                                user_selections__user = request_user)
                qs = qs.exclude(tags__in=ignored_tags)
            else:
                #annotate questions tagged with ignored tags
                qs = qs.extra(
                            select = SortedDict([
                                (
                                    'ignored_score', 
                                    'SELECT COUNT(1) FROM forum_markedtag, question_tags '
                                      + 'WHERE forum_markedtag.user_id = %s '
                                      + 'AND forum_markedtag.tag_id = question_tags.tag_id '
                                      + 'AND forum_markedtag.reason = \'bad\' '
                                      + 'AND question_tags.question_id = question.id'
                                )
                                    ]),
                            select_params = (uid_str, )
                         )
            # get the list of interesting and ignored tags (interesting_tag_names, ignored_tag_names) = (None, None)
            pt = MarkedTag.objects.filter(user=request_user)
            meta_data['interesting_tag_names'] = pt.filter(reason='good').values_list('tag__name', flat=True)
            meta_data['ignored_tag_names'] = pt.filter(reason='bad').values_list('tag__name', flat=True)

        qs = qs.select_related(depth=1)
        #todo: fix orderby here
        orderby = QUESTION_ORDER_BY_MAP[sort_method]
        if orderby:
            #relevance will be ignored here
            qs = qs.order_by(orderby)
        qs = qs.distinct()
        return qs, meta_data

    def get_question_and_answer_contributors(self, question_list):
        answer_list = []
        question_list = list(question_list)#important for MySQL, b/c it does not support
        #the query in return statement below
        for q in question_list:
            answer_list.extend(list(q.answers.all()))
        return User.objects.filter(
                                    Q(questions__in=question_list) \
                                    | Q(answers__in=answer_list)
                                   ).distinct().order_by('?')

    def update_tags(self, question, tagnames, user):
        """
        Updates Tag associations for a question to match the given
        tagname string.

        Returns ``True`` if tag usage counts were updated as a result,
        ``False`` otherwise.
        """

        current_tags = list(question.tags.all())
        current_tagnames = set(t.name for t in current_tags)
        updated_tagnames = set(t for t in tagnames.split(' ') if t)
        modified_tags = []

        removed_tags = [t for t in current_tags
                        if t.name not in updated_tagnames]
        if removed_tags:
            modified_tags.extend(removed_tags)
            question.tags.remove(*removed_tags)

        added_tagnames = updated_tagnames - current_tagnames
        if added_tagnames:
            added_tags = Tag.objects.get_or_create_multiple(added_tagnames,
                                                            user)
            modified_tags.extend(added_tags)
            question.tags.add(*added_tags)

        if modified_tags:
            Tag.objects.update_use_counts(modified_tags)
            return True

        return False

    #todo: why not make this into a method of class Question?
    #      also it is actually strange - why do we need the answer_count
    #      field if the count depends on who is requesting this?
    def update_answer_count(self, question):
        """
        Executes an UPDATE query to update denormalised data with the
        number of answers the given question has.
        """

        # for some reasons, this Answer class failed to be imported,
        # although we have imported all classes from models on top.
        from answer import Answer
        self.filter(id=question.id).update(
            answer_count=Answer.objects.get_answers_from_question(question).filter(deleted=False).count())

    def update_view_count(self, question):
        """
        update counter+1 when user browse question page
        """
        self.filter(id=question.id).update(view_count = question.view_count + 1)

    def update_favorite_count(self, question):
        """
        update favourite_count for given question
        """
        self.filter(id=question.id).update(favourite_count = FavoriteQuestion.objects.filter(question=question).count())

    def get_similar_questions(self, question):
        """
        Get 10 similar questions for given one.
        This will search the same tag list for give question(by exactly same string) first.
        Questions with the individual tags will be added to list if above questions are not full.
        """
        #print datetime.datetime.now()

        manager = self

        def get_data():
            questions = list(manager.filter(tagnames = question.tagnames, deleted=False).all())

            tags_list = question.tags.all()
            for tag in tags_list:
                extend_questions = manager.filter(tags__id = tag.id, deleted=False)[:50]
                for item in extend_questions:
                    if item not in questions and len(questions) < 10:
                        questions.append(item)

            #print datetime.datetime.now()
            return questions

        return LazyList(get_data)

class Question(Content, DeletableContent):
    title    = models.CharField(max_length=300)
    tags     = models.ManyToManyField('Tag', related_name='questions')
    answer_accepted = models.BooleanField(default=False)
    closed          = models.BooleanField(default=False)
    closed_by       = models.ForeignKey(User, null=True, blank=True, related_name='closed_questions')
    closed_at       = models.DateTimeField(null=True, blank=True)
    close_reason    = models.SmallIntegerField(choices=CLOSE_REASONS, null=True, blank=True)
    followed_by     = models.ManyToManyField(User, related_name='followed_questions')

    # Denormalised data
    answer_count         = models.PositiveIntegerField(default=0)
    view_count           = models.PositiveIntegerField(default=0)
    favourite_count      = models.PositiveIntegerField(default=0)
    last_activity_at     = models.DateTimeField(default=datetime.datetime.now)
    last_activity_by     = models.ForeignKey(User, related_name='last_active_in_questions')
    tagnames             = models.CharField(max_length=125)
    summary              = models.CharField(max_length=180)

    favorited_by         = models.ManyToManyField(User, through='FavoriteQuestion', related_name='favorite_questions') 

    objects = QuestionManager()

    class Meta(Content.Meta):
        db_table = u'question'

    def delete(self):
        super(Question, self).delete()
        try:
            ping_google()
        except Exception:
            logging.debug('problem pinging google did you register you sitemap with google?')

    def retag(self, retagged_by=None, retagged_at=None, tagnames=None):
        if None in (retagged_by, retagged_at, tagnames):
            raise Exception('arguments retagged_at, retagged_by and tagnames are required')
        # Update the Question itself
        self.tagnames = tagnames
        self.last_edited_at = retagged_at
        self.last_activity_at = retagged_at
        self.last_edited_by = retagged_by
        self.last_activity_by = retagged_by

        # Update the Question's tag associations
        tags_updated = self.objects.update_tags(self,
            form.cleaned_data['tags'], request.user)

        # Create a new revision
        latest_revision = self.get_latest_revision()
        QuestionRevision.objects.create(
            question   = self,
            title      = latest_revision.title,
            author     = retagged_by,
            revised_at = retagged_at,
            tagnames   = tagnames,
            summary    = CONST['retagged'],
            text       = latest_revision.text
        )
        # send tags updated singal
        tags_updated.send(sender=question.__class__, question=self)

    def apply_edit(self, edited_at=None, edited_by=None, title=None,\
                    text=None, comment=None, tags=None, wiki=False):

        latest_revision = self.get_latest_revision()
        #a hack to allow partial edits - important for SE loader
        if title is None:
            title = self.title
        if text is None:
            text = latest_revision.text
        if tags is None:
            tags = latest_revision.tagnames

        if edited_by is None:
            raise Exception('parameter edited_by is required')

        if edited_at is None:
            edited_at = datetime.datetime.now()

        #todo: have this copy-paste in few places
        html = sanitize_html(markdowner.convert(text))
        question_summary = strip_tags(html)[:120]

        # Update the Question itself
        self.title = title
        self.last_edited_at = edited_at
        self.last_activity_at = edited_at
        self.last_edited_by = edited_by
        self.last_activity_by = edited_by
        self.tagnames = tags
        self.summary = question_summary
        self.html = html
        self.text = text

        #wiki is an eternal trap whence there is no exit
        if self.wiki == False and wiki == True:
            self.wiki = True

        self.save()

        # Update the Question tag associations
        if latest_revision.tagnames != tags:
            tags_updated = Question.objects.update_tags(self, tags, edited_by)

        # Create a new revision
        self.add_revision(
            author = edited_by,
            text = text,
            revised_at = edited_at,
            comment = comment,
        )

    def add_revision(self,author=None, text=None, comment=None, revised_at=None):
        if None in (author, text, comment):
            raise Exception('author, text and revised_at are required arguments')
        rev_no = self.revisions.all().count() + 1
        if comment in (None, ''):
            if rev_no == 1:
                comment = CONST['default_version']
            else:
                comment = 'No.%s Revision' % rev_no
            
        return QuestionRevision.objects.create(
            question   = self,
            revision   = rev_no,
            title      = self.title,
            author     = author,
            revised_at = revised_at,
            tagnames   = self.tagnames,
            summary    = comment,
            text       = text
        )

    def save(self, **kwargs):
        """
        Overridden to manually manage addition of tags when the object
        is first saved.

        This is required as we're using ``tagnames`` as the sole means of
        adding and editing tags.
        """
        initial_addition = (self.id is None)
        
        super(Question, self).save(**kwargs)

        if initial_addition:
            tags = Tag.objects.get_or_create_multiple(self.tagname_list(),
                                                      self.author)
            self.tags.add(*tags)
            Tag.objects.update_use_counts(tags)

    def tagname_list(self):
        """Creates a list of Tag names from the ``tagnames`` attribute."""
        return [name for name in self.tagnames.split(u' ')]

    def tagname_meta_generator(self):
        return u','.join([unicode(tag) for tag in self.tagname_list()])

    def get_absolute_url(self):
        return '%s%s' % (reverse('question', args=[self.id]), django_urlquote(slugify(self.title)))

    def has_favorite_by_user(self, user):
        if not user.is_authenticated():
            return False

        return FavoriteQuestion.objects.filter(question=self, user=user).count() > 0

    def get_answer_count_by_user(self, user_id):
        from answer import Answer
        query_set = Answer.objects.filter(author__id=user_id)
        return query_set.filter(question=self).count()

    def get_question_title(self):
        if self.closed:
            attr = CONST['closed']
        elif self.deleted:
            attr = CONST['deleted']
        else:
            attr = None
        if attr is not None:
            return u'%s %s' % (self.title, attr)
        else:
            return self.title

    def get_revision_url(self):
        return reverse('question_revisions', args=[self.id])

    def get_last_update_info(self):
        when, who = self.post_get_last_update_info()

        answers = self.answers.all()
        if len(answers) > 0:
            for a in answers:
                a_when, a_who = a.post_get_last_update_info()
                if a_when > when:
                    when = a_when
                    who = a_who

        return when, who

    def get_update_summary(self,last_reported_at=None,recipient_email=''):
        edited = False
        if self.last_edited_at and self.last_edited_at > last_reported_at:
            if self.last_edited_by.email != recipient_email:
                edited = True
        comments = []
        for comment in self.comments.all():
            if comment.added_at > last_reported_at and comment.user.email != recipient_email:
                comments.append(comment)
        new_answers = []
        answer_comments = []
        modified_answers = []
        commented_answers = []
        import sets
        commented_answers = sets.Set([])
        for answer in self.answers.all():
            if (answer.added_at > last_reported_at and answer.author.email != recipient_email):
                new_answers.append(answer)
            if (answer.last_edited_at
                and answer.last_edited_at > last_reported_at
                and answer.last_edited_by.email != recipient_email):
                modified_answers.append(answer)
            for comment in answer.comments.all():
                if comment.added_at > last_reported_at and comment.user.email != recipient_email:
                    commented_answers.add(answer)
                    answer_comments.append(comment)

        #create the report
        if edited or new_answers or modified_answers or answer_comments:
            out = []
            if edited:
                out.append(_('%(author)s modified the question') % {'author':self.last_edited_by.username})
            if new_answers:
                names = sets.Set(map(lambda x: x.author.username,new_answers))
                people = ', '.join(names)
                out.append(_('%(people)s posted %(new_answer_count)s new answers') \
                                % {'new_answer_count':len(new_answers),'people':people})
            if comments:
                names = sets.Set(map(lambda x: x.user.username,comments))
                people = ', '.join(names)
                out.append(_('%(people)s commented the question') % {'people':people})
            if answer_comments:
                names = sets.Set(map(lambda x: x.user.username,answer_comments))
                people = ', '.join(names)
                if len(commented_answers) > 1:
                    out.append(_('%(people)s commented answers') % {'people':people})
                else:
                    out.append(_('%(people)s commented an answer') % {'people':people})
            url = settings.APP_URL + self.get_absolute_url()
            retval = '<a href="%s">%s</a>:<br>\n' % (url,self.title)
            out = map(lambda x: '<li>' + x + '</li>',out)
            retval += '<ul>' + '\n'.join(out) + '</ul><br>\n'
            return retval
        else:
            return None

    def __unicode__(self):
        return self.title

        
class QuestionView(models.Model):
    question = models.ForeignKey(Question, related_name='viewed')
    who = models.ForeignKey(User, related_name='question_views')
    when = models.DateTimeField()

    class Meta:
        app_label = 'forum'

class FavoriteQuestion(models.Model):
    """A favorite Question of a User."""
    question      = models.ForeignKey(Question)
    user          = models.ForeignKey(User, related_name='user_favorite_questions')
    added_at      = models.DateTimeField(default=datetime.datetime.now)

    class Meta:
        app_label = 'forum'
        db_table = u'favorite_question'
    def __unicode__(self):
        return '[%s] favorited at %s' %(self.user, self.added_at)

class QuestionRevision(ContentRevision):
    """A revision of a Question."""
    question   = models.ForeignKey(Question, related_name='revisions')
    title      = models.CharField(max_length=300)
    tagnames   = models.CharField(max_length=125)

    class Meta(ContentRevision.Meta):
        db_table = u'question_revision'
        ordering = ('-revision',)

    def get_question_title(self):
        return self.question.title

    def get_absolute_url(self):
        #print 'in QuestionRevision.get_absolute_url()'
        return reverse('question_revisions', args=[self.question.id])

    def save(self, **kwargs):
        """Looks up the next available revision number."""
        if not self.revision:
            self.revision = QuestionRevision.objects.filter(
                question=self.question).values_list('revision',
                                                    flat=True)[0] + 1
        super(QuestionRevision, self).save(**kwargs)

    def __unicode__(self):
        return u'revision %s of %s' % (self.revision, self.title)

class AnonymousQuestion(AnonymousContent):
    title = models.CharField(max_length=300)
    tagnames = models.CharField(max_length=125)

    def publish(self,user):
        added_at = datetime.datetime.now()
        Question.objects.create_new(
                                title=self.title,
                                author=user,
                                added_at=added_at,
                                wiki=self.wiki,
                                tagnames=self.tagnames,
                                text=self.text,
                                )
        self.delete()

from answer import Answer, AnswerManager
