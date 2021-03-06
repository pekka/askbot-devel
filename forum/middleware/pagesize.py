# used in questions
QUESTIONS_PAGE_SIZE = 10
class QuestionsPageSizeMiddleware(object):
    def process_request(self, request):
        # Set flag to False by default. If it is equal to True, then need to be saved.
        page_size_changed = False
        # get page_size from session, if failed then get default value
        user_page_size = request.session.get("page_size", QUESTIONS_PAGE_SIZE)
        # set page_size equal to logon user specified value in database
        if request.user.is_authenticated() and request.user.questions_per_page > 0:
            user_page_size = request.user.questions_per_page

        try:
            # get new page_size from UI selection
            page_size = int(request.GET.get('page_size', user_page_size))
            if page_size <> user_page_size:
                page_size_changed = True

        except ValueError:
            page_size  = user_page_size
        
        # save this page_size to user database
        if page_size_changed:
            if request.user.is_authenticated():
                user = request.user
                user.questions_per_page = page_size
                user.save()
        # put page_size into session
        request.session["page_size"] = page_size

    def process_exception(self,request,exception):
        import logging
        logging.debug('have exception %s' % str(exception))
